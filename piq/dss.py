r"""Implemetation of DCT Subbands Similarity
Code is based on MATLAB version for computations in pixel domain
https://fr.mathworks.com/matlabcentral/fileexchange/\
    53708-dct-subband-similarity-index-for-measuring-image-quality
References:
    http://sipl.eelabs.technion.ac.il/wp-content/uploads/\
    sites/6/2016/09/paper15-Image-Quality-Assessment-Based-on-DCT-Subband-Similarity.pdf
"""
import math
import functools
from typing import Union

import torch
import torch.nn.functional as F

from torch.nn.modules.loss import _Loss

from piq.utils import _adjust_dimensions, _validate_input
from piq.functional import gaussian_filter, rgb2yiq

from skimage.io import imread


def dss(x: torch.Tensor, y: torch.Tensor, reduction: str = 'mean',
        data_range: Union[int, float] = 1.0, dct_size: int = 8,
        sigma_weight: float = 1.55, kernel_size: int = 3,
        sigma_similarity: float = 1.5, percentile: float = 0.05,) -> torch.Tensor:
    r"""Compute DCT Subband Similarity index for a batch of images.

    Args:
        x: Predicted images. Shape (H, W), (C, H, W) or (N, C, H, W).
        y: Target images. Shape (H, W), (C, H, W) or (N, C, H, W).
        reduction: Reduction over samples in batch: "mean"|"sum"|"none"
        data_range: Value range of input images (usually 1.0 or 255). Default: 1.0
        dct_size: Size of blocks in 2D Discrete Cosine Transform
        sigma_weight: STD of gaussian that determines the proportion of weight given to low freq and high freq.
            Default: 1.55
        kernel_size: Size of gaussian kernel for computing subband similarity. Default: 3
        sigma_similarity: STD of gaussian kernel for computing subband similarity. Default: 1.5
        percentile: % in [0,1] of worst similarity scores which should be kept. Default: 0.05
    Returns:
        DSS: Index of similarity betwen two images. In [0, 1] interval.
    Note:
        This implementation is based on the original MATLAB code (see header).

    """
    if sigma_weight == 0 or sigma_similarity == 0:
        raise ValueError('Gaussian sigmas must not be null.')

    if percentile < 0 or percentile > 1:
        raise ValueError('Percentile must be in [0,1]')

    _validate_input(input_tensors=(x, y), allow_5d=False)
    x, y = _adjust_dimensions(input_tensors=(x, y))

    # Rescale to [0, 255] range, because all constant are calculated for this factor
    x = (x / float(data_range)) * 255
    y = (y / float(data_range)) * 255

    num_channels = x.size(1)
    # Use luminance channel in case of RGB images (Y from YIQ or YCrCb)
    if num_channels == 3:
        x_lum = rgb2yiq(x)[:, : 1]
        y_lum = rgb2yiq(y)[:, : 1]

    else:
        x_lum = x
        y_lum = y

    # Crop images size to the closest multiplication of 8
    rows, cols = x_lum.size()[-2:]
    rows = 8*(rows//8)
    cols = 8*(cols//8)
    x_lum = x_lum[:, :, 0:rows, 0:cols]
    y_lum = y_lum[:, :, 0:rows, 0:cols]

    # Channel decomposition for both images by 8x8 2D DCT
    dct_x = _dct_decomp(x_lum, dct_size)
    dct_y = _dct_decomp(y_lum, dct_size)

    # Create a Gaussian window that will be used to weight subbands scores
    r = torch.arange(1, 9)
    Y,X = torch.meshgrid(r, r)
    distance = torch.sqrt((X - 0.5)**2 + (Y - 0.5)**2)
    weight = torch.exp(- (distance**2 / (2 * sigma_weight**2) ))

    # Compute similarity between each subband in img1 and img2
    subband_sim_matrix = torch.zeros((8, 8))
    thres = 1e-2
    for m in range(8):
        for n in range(8):
            first_term = (m==0 and n==0) # boolean

            if weight[m,n] < thres: # Skip subbands with very small weight
                weight[m,n] = 0
                continue

            subband_sim_matrix[m,n] = _subband_similarity(
                dct_x[:, :, m::8, n::8],
                dct_y[:, :, m::8, n::8],
                first_term, kernel_size, sigma_similarity, percentile)

    # Weight subbands similarity scores
    dss = torch.sum(torch.sum(subband_sim_matrix * (weight / torch.sum(weight))))
    return dss


# Compute similarity between two subbbands
def _subband_similarity(x: torch.Tensor, y: torch.Tensor, first_term: bool,
                        kernel_size: int = 3, sigma: float = 1.5,
                        percentile: float = 0.05) -> torch.Tensor:
    r"""Compute similarity between 2 subbands

    Args:
        x: First input subband. Shape (N, 1, H, W).
        y: Second input subband. Shape (N, 1, H, W).
        first_term: whether this is is the first element of subband sim matrix to be calculated
        kernel_size: Size of gaussian kernel for computing local variance. Default: 3
        sigma: STD of gaussian kernel for computing local variance. Default: 1.5
        percentile: % in [0,1] of worst similarity scores which should be kept. Default: 0.05
    Returns:
        DSS: Index of similarity betwen two images. In [0, 1] interval.
    Note:
        This implementation is based on the original MATLAB code (see header).

    """
    # C takes value of DC or AC coefficient depending on stage
    DC_coeff, AC_coeff = (1000, 300)
    C = DC_coeff if first_term else AC_coeff

    # Compute local variance
    kernel = gaussian_filter(kernel_size=kernel_size, sigma=sigma)
    kernel = kernel.view(1, 1, kernel_size, kernel_size).to(x)
    mu_x = F.conv2d(x, kernel, padding=kernel_size//2)
    mu_y = F.conv2d(y, kernel, padding=kernel_size//2)

    sigma_xx = F.conv2d(x * x, kernel, padding=kernel_size//2) - mu_x * mu_x
    sigma_yy = F.conv2d(y * y, kernel, padding=kernel_size//2) - mu_y * mu_y
    sigma_xx[sigma_xx < 0] = 0
    sigma_yy[sigma_yy < 0] = 0
    left_term = (2 * torch.sqrt(sigma_xx * sigma_yy) + C) / (sigma_xx + sigma_yy + C)

    # Spatial pooling of worst scores
    percentile_index = round(percentile * (left_term.size()[-2] * left_term.size()[-1]))
    sorted_left = torch.sort(left_term.flatten()).values
    similarity = torch.mean(sorted_left[:percentile_index])

    # For DC, multiply by a right term
    if first_term:
        sigma_xy = F.conv2d(x * y, kernel, padding=kernel_size//2) - mu_x * mu_y
        right_term = ((sigma_xy + C) / (torch.sqrt(sigma_xx * sigma_yy) + C))
        sorted_right = torch.sort(right_term.flatten()).values
        similarity *= torch.mean(sorted_right[:percentile_index])

    return similarity


def _dct_matrix(N: int) -> torch.Tensor:
    r"""
    Computes the matrix coefficients for DCT transform
    Following this formula https://fr.mathworks.com/help/images/discrete-cosine-transform.html

    Args:
        N: size of DCT matrix to create (N, N)
    """
    p = torch.arange(1,N).reshape((N-1, 1))
    q = torch.arange(1,2*N,2)
    return torch.cat((
        math.sqrt(1/N)*torch.ones((1,N)),
        math.sqrt(2/N) * torch.cos(math.pi / (2 * N) * p * q)), 0)


def _dct_decomp(x: torch.Tensor, N: int = 8) -> torch.Tensor:
    r"""
    Computes 2D Discrete Cosine Transform on 8x8 blocks of an image

    Args:
        x: input image. Shape (Bs, 1, H, W)
        N: size of DCT performed. Default: 8
    Returns:
        decomp: the result of DCT on NxN blocks of the image, same shape.
    Note:
        Inspired by https://gitlab.com/Queuecumber/torchjpeg

    """
    bs, _, h, w = x.size()
    x = x.view(bs, 1, h, w)

    # make NxN blocs out of image
    blocks = F.unfold(x, kernel_size=(N, N), stride=(N, N)) # shape (1, NxN, block_num)
    blocks = blocks.transpose(1, 2)
    blocks = blocks.view(bs, 1, -1, N, N) # shape (bs, 1, block_num, N, N)

    # apply DCT transform
    coeffs = _dct_matrix(N)

    if x.is_cuda:
        coeffs = coeffs.cuda()

    blocks = coeffs @ blocks @ coeffs.t() # @ does operation on last 2 channels only

    # Reconstruct image
    blocks = blocks.reshape(bs, -1, N ** 2)
    blocks = blocks.transpose(1, 2)
    blocks = F.fold(blocks, output_size=x.size()[-2:], kernel_size=(N, N), stride=(N, N))
    decomp = blocks.reshape(bs, 1, x.size()[-2], x.size()[-1])

    return decomp


if __name__ == "__main__":
    goldhill = torch.Tensor(imread('tests/assets/goldhill.gif'))
    goldhill_jpeg = torch.Tensor(imread('tests/assets/goldhill_jpeg.gif'))
    print(dss(goldhill, goldhill_jpeg, data_range=255, reduction='none'))


