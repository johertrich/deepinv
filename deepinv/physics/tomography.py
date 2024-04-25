import torch
from torch import nn
from deepinv.physics.forward import LinearPhysics

from deepinv.physics.functional import Radon, IRadon, RampFilter
from deepinv.physics import adjoint_function


class Tomography(LinearPhysics):
    r"""
    (Computed) Tomography operator.

    The Radon transform is the integral transform which takes a square image :math:`x` defined on the plane to a function
    :math:`y=Rx` defined on the (two-dimensional) space of lines in the plane, whose value at a particular line is equal
    to the line integral of the function over that line.

    .. note::

        The pseudo-inverse is computed using the filtered back-projection algorithm with a Ramp filter.
        This is not the exact linear pseudo-inverse of the Radon transform, but it is a good approximation which is
        robust to noise.

    .. note::

        The measurements are not normalized by the image size, thus the norm of the operator depends on the image size.

    .. warning::

        The adjoint operator has small numerical errors due to interpolation.

    :param int, torch.tensor angles: These are the tomography angles. If the type is ``int``, the angles are sampled uniformly between 0 and 360 degrees.
        If the type is ``torch.tensor``, the angles are the ones provided (e.g., ``torch.linspace(0, 180, steps=10)``).
    :param int img_width: width/height of the square image input.
    :param bool circle: If ``True`` both forward and backward projection will be restricted to pixels inside a circle
        inscribed in the square image.
    :param bool parallel_computation: if True, all projections are performed in parallel. Requires more memory but is faster on GPUs.
    :param bool fan_beam: if True, use fan beam geometry, if False use parallel beam
    :param dict fan_parameters: only used if fan_beam is True. Contains the parameters defining the scanning geometry. The dict should contain the keys
        - "pixel_spacing" defining the distance between two pixels in the image
        - "source_radius" distance between the x-ray source and the rotation axis (middle of the image)
        - "detector_radius" distance between the x-ray detector and the rotation axis (middle of the image)
        - "n_detector_pixels" number of pixels of the detector
        - "detector_spacing" distance between two pixels on the detector
    :param str device: gpu or cpu.

    |sep|

    :Examples:

        Tomography operator with defined angles for 3x3 image:

        >>> from deepinv.physics import Tomography
        >>> seed = torch.manual_seed(0)  # Random seed for reproducibility
        >>> x = torch.randn(1, 1, 4, 4)  # Define random 4x4 image
        >>> angles = torch.linspace(0, 45, steps=3)
        >>> physics = Tomography(angles=angles, img_width=4, circle=True)
        >>> physics(x)
        tensor([[[[ 0.1650,  1.2640,  1.6995],
                  [-0.4860,  0.2674,  0.9971],
                  [ 0.9002, -0.3856, -0.9360],
                  [-2.4882, -2.1068, -2.5720]]]])

        Tomography operator with 3 uniformly sampled angles in [0, 360] for 3x3 image:

        >>> from deepinv.physics import Tomography
        >>> seed = torch.manual_seed(0)  # Random seed for reproducibility
        >>> x = torch.randn(1, 1, 4, 4)  # Define random 4x4 image
        >>> physics = Tomography(angles=3, img_width=4, circle=True)
        >>> physics(x)
        tensor([[[[ 0.1650,  1.9493,  1.9897],
                  [-0.4860,  0.7137, -1.6536],
                  [ 0.9002, -0.8457, -0.1666],
                  [-2.4882, -2.7340, -0.9793]]]])


    """

    def __init__(
        self,
        angles,
        img_width,
        circle=False,
        parallel_computation=True,
        fan_beam=False,
        fan_parameters=None,
        device=torch.device("cpu"),
        dtype=torch.float,
        **kwargs,
    ):
        super().__init__(**kwargs)

        if isinstance(angles, int) or isinstance(angles, float):
            theta = torch.nn.Parameter(
                torch.linspace(0, 180, steps=angles + 1, device=device)[:-1],
                requires_grad=False,
            ).to(device)
        else:
            theta = torch.nn.Parameter(angles, requires_grad=False).to(device)

        self.fan_beam = fan_beam
        self.img_width = img_width
        self.device = device
        self.dtype = dtype
        self.radon = Radon(
            img_width,
            theta,
            circle=circle,
            parallel_computation=parallel_computation,
            fan_beam=fan_beam,
            fan_parameters=fan_parameters,
            device=device,
            dtype=dtype,
        ).to(device)
        if not self.fan_beam:
            self.iradon = IRadon(
                img_width,
                theta,
                circle=circle,
                parallel_computation=parallel_computation,
                device=device,
                dtype=dtype,
            ).to(device)
        else:
            self.filter = RampFilter(dtype=dtype, device=device)

    def A(self, x, **kwargs):
        if self.img_width is None:
            self.img_width = x.shape[-1]
        return self.radon(x)

    def A_dagger(self, y, **kwargs):
        if self.fan_beam:
            y = self.filter(y)
            return self.A_adjoint(y, **kwargs)
        else:
            return self.iradon(y)

    def A_adjoint(self, y, **kwargs):
        if self.fan_beam:
            assert (
                not self.img_width is None
            ), "Image size unknown. Apply forward operator or add it for initialization."
            adj = adjoint_function(
                self.A,
                (y.shape[0], y.shape[1], self.img_width, self.img_width),
                device=self.device,
                dtype=self.dtype,
            )
            return adj(y)
        else:
            return self.iradon(y, filtering=False)
