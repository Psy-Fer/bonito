import os
import torch
import numpy as np
from io import BytesIO
from collections import namedtuple
from bonito.crf.model import CTC_CRF
from torch.nn import Conv2d, BatchNorm2d

try: from openvino.inference_engine import IECore, StatusCode
except ImportError: pass


def load_openvino_model(model, dirname):
    package = model.config['model']['package']
    if package == 'bonito.ctc':
        return OpenVINOCTCModel(model, dirname)
    elif package == 'bonito.crf':
        return OpenVINOCRFModel(model, dirname)
    else:
        raise Exception('Unknown model configuration: ' + package)


def convert_to_2d(model):
    for name, l in model.named_children():
        layer_type = l.__class__.__name__
        if layer_type == 'Conv1d':
            new_layer = Conv2d(
                l.in_channels, l.out_channels,
                (1, l.kernel_size[0]), (1, l.stride[0]),
                (0, l.padding[0]), (1, l.dilation[0]),
                l.groups, False if l.bias is None else True, l.padding_mode
            )
            params = l.state_dict()
            params['weight'] = params['weight'].unsqueeze(2)
            new_layer.load_state_dict(params)
            setattr(model, name, new_layer)
        elif layer_type == 'BatchNorm1d':
            new_layer = BatchNorm2d(l.num_features, l.eps)
            new_layer.load_state_dict(l.state_dict())
            new_layer.eval()
            setattr(model, name, new_layer)
        elif layer_type == 'Permute':
            dims_2d = []
            # 1D to 2D: i.e. (2, 0, 1) -> (2, 3, 0, 1)
            for d in l.dims:
                assert(d <= 2)
                dims_2d.append(d)
                if d == 2:
                    dims_2d.append(3)
            l.dims = dims_2d
        else:
            convert_to_2d(l)


class OpenVINOModel:

    def __init__(self, model, dirname):
        self.model = model
        self.alphabet = model.alphabet
        self.parameters = model.parameters
        self.stride = model.stride
        self.net = None
        self.exec_net = None
        self.dirname = dirname
        self.ie = IECore()

    def eval(self):
        pass

    def half(self):
        return self

    @property
    def config(self):
        return self.model.config

    def to(self, device):
        self.device = str(device).upper()

    def init_model(self, model, inp_shape):
        """
        Call this method once to initialize executable network
        """
        # First, we try to check if there is IR on disk. If not - load model in runtime
        xml_path, bin_path = [os.path.join(self.dirname, 'model') + ext for ext in ['.xml', '.bin']]
        if os.path.exists(xml_path) and os.path.exists(bin_path):
            self.net = self.ie.read_network(xml_path, bin_path)
        else:
            # Convert model to ONNX buffer
            buf = BytesIO()
            inp = torch.randn(inp_shape)
            torch.onnx.export(model, inp, buf, input_names=['input'], output_names=['output'], opset_version=11)

            # Import network from memory buffer
            self.net = self.ie.read_network(buf.getvalue(), b'', init_from_buffer=True)

        # Load model to device
        config = {}
        if self.device == 'CPU':
            config={'CPU_THROUGHPUT_STREAMS': 'CPU_THROUGHPUT_AUTO'}
        self.exec_net = self.ie.load_network(self.net, self.device, config=config, num_requests=0)

    def process(self, data):

        data = data.float()
        batch_size = data.shape[0]
        inp_shape = list(data.shape)
        inp_shape[0] = 1  # We will run the batch asynchronously

        # List that maps infer requests to index of processed chunk from batch.
        # -1 means that request has not been started yet.
        infer_request_input_id = [-1] * len(self.exec_net.requests)
        out_shape = self.net.outputs['output'].shape

        if len(out_shape) == 3:
            out_shape = [1, *out_shape]

        # CTC network produces 1xWxNxC
        output = np.zeros([out_shape[-3], batch_size, out_shape[-1]], dtype=np.float32)

        for inp_id in range(batch_size):
            # Get idle infer request
            infer_request_id = self.exec_net.get_idle_request_id()
            if infer_request_id < 0:
                status = self.exec_net.wait(num_requests=1)
                if status != StatusCode.OK:
                    raise Exception("Wait for idle request failed!")
                infer_request_id = self.exec_net.get_idle_request_id()
                if infer_request_id < 0:
                    raise Exception("Invalid request id!")

            out_id = infer_request_input_id[infer_request_id]
            request = self.exec_net.requests[infer_request_id]

            # Copy output prediction
            if out_id != -1:
                output[:, out_id:out_id + 1] = request.output_blobs['output'].buffer

            # Start this request on new data
            infer_request_input_id[infer_request_id] = inp_id
            request.async_infer({'input': data[inp_id]})
            inp_id += 1

        # Wait for the rest of requests
        status = self.exec_net.wait()
        if status != StatusCode.OK:
            raise Exception("Wait for idle request failed!")

        for infer_request_id, out_id in enumerate(infer_request_input_id):
            if out_id == -1:
                continue
            request = self.exec_net.requests[infer_request_id]
            output[:, out_id:out_id + 1] = request.output_blobs['output'].buffer

        return torch.tensor(output)


class OpenVINOCTCModel(OpenVINOModel):

    def __init__(self, model, dirname):
        super().__init__(model, dirname)

    def __call__(self, data):
        data = data.unsqueeze(2)  # 1D->2D
        if self.exec_net is None:
            convert_to_2d(self.model)
            self.init_model(self.model, [1, 1, 1, data.shape[-1]])
        return self.process(data)

    def decode(self, x, beamsize=5, threshold=1e-3, qscores=False, return_path=False):
        return self.model.decode(
            x, beamsize=beamsize, threshold=threshold, qscores=qscores, return_path=return_path
        )


def grad(f, x):
    x = x.detach().requires_grad_()
    with torch.enable_grad():
        y = f(x)
    return torch.autograd.grad(y, x)[0].detach()


def max_grad(x, dim=0):
    return torch.zeros_like(x).scatter_(dim, x.argmax(dim, True), 1.0)


semiring = namedtuple('semiring', ('zero', 'one', 'mul', 'sum', 'dsum'))
Log = semiring(zero=-1e38, one=0., mul=torch.add, sum=torch.logsumexp, dsum=torch.softmax)
Max = semiring(zero=-1e38, one=0., mul=torch.add, sum=(lambda x, dim=0: torch.max(x, dim=dim)[0]), dsum=max_grad)


def scan(Ms, idx, v0, S:semiring=Log):
    T, N, C, NZ = Ms.shape
    alpha = Ms.new_full((T + 1, N, C), S.zero)
    alpha[0] = v0
    for t in range(T):
        alpha[t+1] = S.sum(S.mul(Ms[t], alpha[t, :, idx]), dim=-1)
    return alpha


class LogZ(torch.autograd.Function):

    @staticmethod
    def forward(ctx, Ms, idx, v0, vT, scan, S:semiring):
        alpha = scan(Ms, idx, v0, S)
        ctx.save_for_backward(alpha, Ms, idx, vT)
        ctx.semiring, ctx.scan = S, scan
        return S.sum(S.mul(alpha[-1], vT), dim=1)

    @staticmethod
    def backward(ctx, grad):
        alpha, Ms, idx, vT = ctx.saved_tensors
        S, scan = ctx.semiring, ctx.scan
        T, N, C, NZ = Ms.shape
        idx_T = idx.flatten().argsort().reshape(*idx.shape)
        Ms_T = Ms.reshape(T, N, -1)[:, :, idx_T]
        idx_T = torch.div(idx_T, NZ, rounding_mode='floor')
        beta = scan(Ms_T.flip(0), idx_T, vT, S)
        g = S.mul(S.mul(Ms.reshape(T, N, -1), alpha[:-1, :, idx.flatten()]).reshape(T, N, C, NZ), beta[:-1, :, :, None].flip(0))
        g = S.dsum(g.reshape(T, N, -1), dim=2).reshape(T, N, C, NZ)
        return grad[None, :, None, None] * g, None, None, None, None, None


class CTC_CRF_CPU(CTC_CRF):

    def logZ(self, scores, S:semiring=Log):
        T, N, _ = scores.shape
        Ms = scores.reshape(T, N, -1, self.n_base + 1)
        v0 = Ms.new_full((N, self.n_base**(self.state_len)), S.one)
        vT = Ms.new_full((N, self.n_base**(self.state_len)), S.one)
        return LogZ.apply(Ms, self.idx.to(torch.int64), v0, vT, scan, S)

    def forward_scores(self, scores, S: semiring=Log):
        T, N, _ = scores.shape
        Ms = scores.reshape(T, N, -1, self.n_base + 1)
        v0 = Ms.new_full((N, self.n_base**(self.state_len)), S.one)
        return scan(Ms, self.idx.to(torch.int64), v0, S)

    def backward_scores(self, scores, S: semiring=Log):
        T, N, _ = scores.shape
        vT = scores.new_full((N, self.n_base**(self.state_len)), S.one)
        idx_T = self.idx.flatten().argsort().reshape(*self.idx.shape)
        Ms_T = scores[:, :, idx_T]
        idx_T = torch.div(idx_T, self.n_base + 1, rounding_mode='floor')
        return scan(Ms_T.flip(0), idx_T.to(torch.int64), vT, S).flip(0)


class OpenVINOCRFModel(OpenVINOModel):

    def __init__(self, model, dirname):
        super().__init__(model, dirname)
        self.seqdist = CTC_CRF_CPU(model.seqdist.state_len, model.seqdist.alphabet)

    def __call__(self, data):
        if self.exec_net is None:
            self.init_model(self.model.encoder, [1, 1, data.shape[-1]])
        return self.process(data)
