# Owner(s): ["module: inductor"]

import torch

from torch._dynamo import config as dynamo_config
from torch._inductor import config as inductor_config
from torch._inductor.utils import is_big_gpu
from torch.testing import make_tensor
from torch.testing._internal.common_device_type import instantiate_device_type_tests
from torch.testing._internal.common_utils import IS_LINUX, TestCase as TorchTestCase
from torch.testing._internal.inductor_utils import GPU_TYPE, HAS_CUDA, skipCUDAIf


class TestUnbackedSymints(TorchTestCase):
    @skipCUDAIf(not HAS_CUDA, "requires cuda")
    @dynamo_config.patch({"capture_dynamic_output_shape_ops": True})
    def test_expand(self, device):
        def fn(x, y):
            nz = torch.nonzero(x)
            # unbacked symint in nz.size
            x_exp = nz.expand([-1, 128])
            # unbacked symint in target sizes
            y_exp = y.expand([-1, nz.size(0)])
            return x_exp, y_exp

        example_inputs = (
            torch.randn((32), device=device),
            torch.randn((32, 1), device=device),
        )

        actual = torch.compile(fn, fullgraph=True)(*example_inputs)
        expected = fn(*example_inputs)

        torch.testing.assert_close(actual, expected)

    @skipCUDAIf(not HAS_CUDA, "requires cuda")
    @dynamo_config.patch({"capture_dynamic_output_shape_ops": True})
    def test_expand_mismatch(self, device):
        def fn(x):
            nz = x.nonzero()
            return nz.expand([-1, 128])

        x = make_tensor(32, 4, device=device, dtype=torch.float32, exclude_zero=True)
        with self.assertRaises(torch._dynamo.exc.TorchRuntimeError):
            actual = torch.compile(fn, fullgraph=True)(x)

    @skipCUDAIf(not HAS_CUDA, "requires cuda")
    @dynamo_config.patch({"capture_dynamic_output_shape_ops": True})
    def test_autotuning(self, device):
        def fn(x, y):
            nz = torch.nonzero(x)
            # unbacked symint in the GEMM input shape
            a = x.new_ones([nz.size(0), y.size(0)])
            return a @ y

        example_inputs = (
            torch.randn((64), device=device),
            torch.randn((32, 16), device=device),
        )

        with inductor_config.patch(
            {
                "max_autotune_gemm": True,
            }
        ):
            actual = torch.compile(fn, fullgraph=True)(*example_inputs)
            expected = fn(*example_inputs)

        torch.testing.assert_close(actual, expected)

    @skipCUDAIf(not HAS_CUDA, "requires cuda")
    @dynamo_config.patch({"capture_scalar_outputs": True})
    def test_split_with_sizes(self, device):
        def fn(x, y):
            l = y.tolist()
            s = torch.split(x, l)
            d = l[0] + l[1] + l[2]
            return s[0].sum(), d

        example_inputs = (torch.randn((32), device=device), torch.tensor((7, 16, 9)))

        actual = torch.compile(fn, fullgraph=True)(*example_inputs)
        expected = fn(*example_inputs)

        torch.testing.assert_close(actual, expected)

    @skipCUDAIf(not HAS_CUDA, "requires cuda")
    @dynamo_config.patch({"capture_dynamic_output_shape_ops": True})
    def test_view_of_slice(self, device):
        # Tests View.create(slice, size_with_unbacked_symint)
        def fn(x):
            nz = torch.nonzero(x)  # introduce unbacked symint
            squared = nz * nz  # avoid ReinterpretView when lowering Slice
            sliced = torch.ops.aten.slice.Tensor(squared, dim=1, start=-2, end=None)
            view = sliced.unsqueeze(dim=0)
            return view.squeeze(
                dim=0
            )  # make sure no unbacked symint in output's stride

        example_inputs = (torch.randn(1, 1, 1, 1, device=device),)
        actual = torch.compile(fn, fullgraph=True)(*example_inputs)
        expected = fn(*example_inputs)
        torch.testing.assert_close(actual, expected)


instantiate_device_type_tests(
    TestUnbackedSymints, globals(), only_for=(GPU_TYPE, "cpu")
)

if __name__ == "__main__":
    from torch._dynamo.test_case import run_tests

    if IS_LINUX and HAS_CUDA and is_big_gpu(0):
        run_tests()
