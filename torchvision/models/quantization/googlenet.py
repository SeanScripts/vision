import warnings
from functools import partial
from typing import Any, Optional, Union

import torch
import torch.nn as nn
from torch import Tensor
from torch.nn import functional as F

from ...transforms._presets import ImageClassification, InterpolationMode
from .._api import Weights, WeightsEnum
from .._meta import _IMAGENET_CATEGORIES
from .._utils import _ovewrite_named_param, handle_legacy_interface
from ..googlenet import (
    BasicConv2d,
    GoogLeNet,
    GoogLeNet_Weights,
    GoogLeNetOutputs,
    Inception,
    InceptionAux,
)
from .utils import _fuse_modules, _replace_relu, quantize_model


__all__ = [
    "QuantizableGoogLeNet",
    "GoogLeNet_QuantizedWeights",
    "googlenet",
]


class QuantizableBasicConv2d(BasicConv2d):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.relu = nn.ReLU()

    def forward(self, x: Tensor) -> Tensor:
        x = self.conv(x)
        x = self.bn(x)
        x = self.relu(x)
        return x

    def fuse_model(self, is_qat: Optional[bool] = None) -> None:
        _fuse_modules(self, ["conv", "bn", "relu"], is_qat, inplace=True)


class QuantizableInception(Inception):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(conv_block=QuantizableBasicConv2d, *args, **kwargs)  # type: ignore[misc]
        self.cat = nn.quantized.FloatFunctional()

    def forward(self, x: Tensor) -> Tensor:
        outputs = self._forward(x)
        return self.cat.cat(outputs, 1)


class QuantizableInceptionAux(InceptionAux):
    # TODO https://github.com/pytorch/vision/pull/4232#pullrequestreview-730461659
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(conv_block=QuantizableBasicConv2d, *args, **kwargs)  # type: ignore[misc]
        self.relu = nn.ReLU()

    def forward(self, x: Tensor) -> Tensor:
        # aux1: N x 512 x 14 x 14, aux2: N x 528 x 14 x 14
        x = F.adaptive_avg_pool2d(x, (4, 4))
        # aux1: N x 512 x 4 x 4, aux2: N x 528 x 4 x 4
        x = self.conv(x)
        # N x 128 x 4 x 4
        x = torch.flatten(x, 1)
        # N x 2048
        x = self.relu(self.fc1(x))
        # N x 1024
        x = self.dropout(x)
        # N x 1024
        x = self.fc2(x)
        # N x 1000 (num_classes)

        return x


class QuantizableGoogLeNet(GoogLeNet):
    # TODO https://github.com/pytorch/vision/pull/4232#pullrequestreview-730461659
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(  # type: ignore[misc]
            blocks=[QuantizableBasicConv2d, QuantizableInception, QuantizableInceptionAux], *args, **kwargs
        )
        self.quant = torch.ao.quantization.QuantStub()
        self.dequant = torch.ao.quantization.DeQuantStub()

    def forward(self, x: Tensor) -> GoogLeNetOutputs:
        x = self._transform_input(x)
        x = self.quant(x)
        x, aux1, aux2 = self._forward(x)
        x = self.dequant(x)
        aux_defined = self.training and self.aux_logits
        if torch.jit.is_scripting():
            if not aux_defined:
                warnings.warn("Scripted QuantizableGoogleNet always returns GoogleNetOutputs Tuple")
            return GoogLeNetOutputs(x, aux2, aux1)
        else:
            return self.eager_outputs(x, aux2, aux1)

    def fuse_model(self, is_qat: Optional[bool] = None) -> None:
        r"""Fuse conv/bn/relu modules in googlenet model

        Fuse conv+bn+relu/ conv+relu/conv+bn modules to prepare for quantization.
        Model is modified in place.  Note that this operation does not change numerics
        and the model after modification is in floating point
        """

        for m in self.modules():
            if type(m) is QuantizableBasicConv2d:
                m.fuse_model(is_qat)


class GoogLeNet_QuantizedWeights(WeightsEnum):
    IMAGENET1K_FBGEMM_V1 = Weights(
        url="https://download.pytorch.org/models/quantized/googlenet_fbgemm-c00238cf.pth",
        transforms=partial(ImageClassification, crop_size=224),
        meta={
            "task": "image_classification",
            "architecture": "GoogLeNet",
            "publication_year": 2014,
            "num_params": 6624904,
            "size": (224, 224),
            "min_size": (15, 15),
            "categories": _IMAGENET_CATEGORIES,
            "interpolation": InterpolationMode.BILINEAR,
            "backend": "fbgemm",
            "quantization": "Post Training Quantization",
            "recipe": "https://github.com/pytorch/vision/tree/main/references/classification#post-training-quantized-models",
            "unquantized": GoogLeNet_Weights.IMAGENET1K_V1,
            "acc@1": 69.826,
            "acc@5": 89.404,
        },
    )
    DEFAULT = IMAGENET1K_FBGEMM_V1


@handle_legacy_interface(
    weights=(
        "pretrained",
        lambda kwargs: GoogLeNet_QuantizedWeights.IMAGENET1K_FBGEMM_V1
        if kwargs.get("quantize", False)
        else GoogLeNet_Weights.IMAGENET1K_V1,
    )
)
def googlenet(
    *,
    weights: Optional[Union[GoogLeNet_QuantizedWeights, GoogLeNet_Weights]] = None,
    progress: bool = True,
    quantize: bool = False,
    **kwargs: Any,
) -> QuantizableGoogLeNet:
    r"""GoogLeNet (Inception v1) model architecture from
    `"Going Deeper with Convolutions" <http://arxiv.org/abs/1409.4842>`_.

    Note that quantize = True returns a quantized model with 8 bit
    weights. Quantized models only support inference and run on CPUs.
    GPU inference is not yet supported

    Args:
        weights (GoogLeNet_QuantizedWeights or GoogLeNet_Weights, optional): The pretrained
            weights for the model
        progress (bool): If True, displays a progress bar of the download to stderr
        quantize (bool): If True, return a quantized version of the model
    """
    weights = (GoogLeNet_QuantizedWeights if quantize else GoogLeNet_Weights).verify(weights)

    original_aux_logits = kwargs.get("aux_logits", False)
    if weights is not None:
        if "transform_input" not in kwargs:
            _ovewrite_named_param(kwargs, "transform_input", True)
        _ovewrite_named_param(kwargs, "aux_logits", True)
        _ovewrite_named_param(kwargs, "init_weights", False)
        _ovewrite_named_param(kwargs, "num_classes", len(weights.meta["categories"]))
        if "backend" in weights.meta:
            _ovewrite_named_param(kwargs, "backend", weights.meta["backend"])
    backend = kwargs.pop("backend", "fbgemm")

    model = QuantizableGoogLeNet(**kwargs)
    _replace_relu(model)
    if quantize:
        quantize_model(model, backend)

    if weights is not None:
        model.load_state_dict(weights.get_state_dict(progress=progress))
        if not original_aux_logits:
            model.aux_logits = False
            model.aux1 = None  # type: ignore[assignment]
            model.aux2 = None  # type: ignore[assignment]
        else:
            warnings.warn(
                "auxiliary heads in the pretrained googlenet model are NOT pretrained, so make sure to train them"
            )

    return model
