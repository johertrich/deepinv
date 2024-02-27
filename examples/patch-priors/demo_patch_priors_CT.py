r"""
Patch priors for limited-angle computed tomography
====================================================================================================

In this example we use patch priors for limited angle computed tomography. More precisely, we consider the 
inverse problem :math:`y = \mathrm{noisy}(Ax)`, where :math`A` is the discretized Radon transform 
with :math:`100` equispace angles between 20 and 160 degrees.
For the reconstruction, we minimize the variational problem

.. math::
    \begin{equation*}
    \label{eq:min_prob}
    \underset{x}{\arg\min} \quad \lambda \datafid{x}{y} + g(x).
    \end{equation*}

Here, the regularizier :math:`g` is explicitly defined as

.. math::
    \begin{equation*}
    g(x)=\sum_{i\in\mathcal{I}} h(P_i x),
    \end{equation*}

where :math:`P_i` is the linear operator which extracts the :math:`i`-th patch from the image :math:`x` and 
:math:`h` is a regularizer on the space of patches.
We consider the following two choices of :math:`h`:

* The expected patch log-likelihood (EPLL) prior was proposed in `"From learning models of natural image patches to whole image restoration" <https://ieeexplore.ieee.org/document/6126278>`_.
  It sets :math:`h(x)=-\log(p_\theta(x))`, where :math:`p_\theta` is the probability density function of a Gaussian mixture model.
  The parameters :math:`\theta` are estimated a-priori on a (possibly small) data set of training patches using
  an expectation maximization algorithm.
  In contrast to the original paper by Zoran and Weiss, we minimize the arising variational problem by simply applying
  the Adam optimizers. For an example for using the (approximated) half-quadratic splitting algorithm proposed by Zoran
  and Weiss, we refer to the denoising example...

* The patch normalizing flow regularizer (PatchNR) was proposed in `"PatchNR: learning from very few images by patch normalizing flow regularization" <https://iopscience.iop.org/article/10.1088/1361-6420/acce5e/>`_.
  It models :math:`h(x)=-\log(p_{\theta}(x))` as negative log-likelihood function of a probaility density function 
  :math:`p_\theta={\mathcal{T}_\theta}_\#\mathcal{N}(0,I)` which is given as the push-forward measure of a standard
  normal distribution under a normalizing flow (invertible neural network) :math:`\mathcal{T}_\theta`.
"""

import torch
from deepinv.datasets import PatchDataset
from deepinv.models import EPLL, PatchNR
from torch.utils.data import DataLoader
from deepinv import train_normalizing_flow
from deepinv.physics import LogPoissonNoise, Tomography
from deepinv.optim import LogPoissonLikelihood, PatchPrior
from deepinv.utils import cal_psnr, plot
from tqdm import tqdm

device = "cuda" if torch.cuda.is_available() else "cpu"

# %%
# Load training and test images
# -----------------------------------------
# Here, we use downsampled images from the `"LoDoPaB-CT dataset" <https://zenodo.org/records/3384092>`_.
# Moreover, we define the size of the used patches and generate the dataset of patches in the training images.

dataset = torch.load("dataset_small.pt")
train_imgs = dataset["train_imgs"]
test_imgs = dataset["test_imgs"]
img_size = train_imgs.shape[-1]

patch_size = 3
verbose = True
train_dataset = PatchDataset(train_imgs, patch_size=patch_size, transforms=None)

# %%
# Set parameters for EPLL and PatchNR
# -----------------------------------------
# For PatchNR, we choose the number of hidden neurons in the subnetworks and for the training batch size and number of epochs.
# For EPLL, we set the number of mixture components and the maximum number of steps and batch size for fitting the EM algorithm.

patchnr_subnetsize = 128
patchnr_epochs = 5
patchnr_batch_size = 32
patchnr_learning_rate = 1e-4

epll_num_components = 20
epll_max_iter = 20
epll_batch_size = 10000

# %%
# Training / EM algorithm
# -----------------------------------------
# If the parameter retrain is False, we just load pretrained weights. Set the parameter to True for retraining.
# On the cpu, this takes up to a couple of minutes.
# After training, we define the corresponding patch priors

retrain = False
if retrain:
    model_patchnr = PatchNR(
        pretrained_weights=None,
        sub_net_size=patchnr_subnetsize,
        device=device,
        patch_size=patch_size,
    )
    patchnr_dataloader = DataLoader(
        train_dataset, batch_size=patchnr_batch_size, shuffle=True, drop_last=True
    )
    train_normalizing_flow(
        model_patchnr.normalizing_flow,
        patchnr_dataloader,
        epochs=patchnr_epochs,
        learning_rate=patchnr_learning_rate,
        device=device,
        verbose=verbose,
    )

    model_epll = EPLL(
        pretrained_weights=None,
        n_components=epll_num_components,
        patch_size=patch_size,
        device=device,
    )
    epll_dataloader = DataLoader(
        train_dataset, batch_size=epll_batch_size, shuffle=True, drop_last=False
    )
    model_epll.GMM.fit(epll_dataloader, verbose=verbose, max_iters=epll_max_iter)
else:
    model_patchnr = PatchNR(
        pretrained_weights="PatchNR_lodopab_small.pt",
        sub_net_size=patchnr_subnetsize,
        device=device,
        patch_size=patch_size,
    )
    model_epll = EPLL(
        pretrained_weights="GMM_lodopab_small.pt",
        n_components=epll_num_components,
        patch_size=patch_size,
        device=device,
    )

patchnr_prior = PatchPrior(model_patchnr, patch_size=patch_size)
epll_prior = PatchPrior(model_epll, patch_size=patch_size)

# %%
# Definition of forward operator and noise model
# -----------------------------------------------
# The training depends only on the image domain or prior distribution.
# For the reconstruction, we now define forward operator and noise model.
# For the noise model, we use log-Poisson noise as suggested for the LoDoPaB dataset.
# Then, we generate an observation by applying the physics and compute the filtered backprojection.

mu = 1 / 50.0 * (362.0 / img_size)
N0 = 2**10
num_angles = 100
noise_model = LogPoissonNoise(mu=mu, N0=N0)
data_fidelity = LogPoissonLikelihood(mu=mu, N0=N0)
angles = torch.linspace(20, 160, steps=num_angles)
physics = Tomography(
    img_width=img_size, angles=angles, device=device, noise_model=noise_model
)
observation = physics(test_imgs)
fbp = physics.A_dagger(observation)

# %%
# Reconstruction loop
# -----------------------------------------------
# We define a reconstruction loop for minimizing the variational problem using the Adam optimizer.
# As initialization, we choose the filtered backprojection.

optim_steps = 200
lr_variational_problem = 0.02


def minimize_variational_problem(prior, lam):
    imgs = fbp.clone()
    imgs.requires_grad_(True)
    optimizer = torch.optim.Adam([imgs], lr=lr_variational_problem)
    for i in (progress_bar := tqdm(range(optim_steps))):
        optimizer.zero_grad()
        loss = data_fidelity(imgs, observation, physics) + lam * prior(imgs)
        loss.backward()
        optimizer.step()
        progress_bar.set_description("Step {}".format(i + 1))
    return imgs.detach()


# %%
# Run and plot
# -----------------------------------------------
# Finally, we run the reconstruction loop for both priors and plot the results.
# The regularization parameter is roughly choosen by a grid search but not fine-tuned

lam_patchnr = 120.0
lam_epll = 120.0

recon_patchnr = minimize_variational_problem(patchnr_prior, lam_patchnr)
recon_epll = minimize_variational_problem(epll_prior, lam_epll)

psnr_fbp = cal_psnr(fbp, test_imgs)
psnr_patchnr = cal_psnr(recon_patchnr, test_imgs)
psnr_epll = cal_psnr(recon_epll, test_imgs)

print("PSNRs:")
print("Filtered Backprojection: {0:.2f}".format(psnr_fbp))
print("EPLL: {0:.2f}".format(psnr_epll))
print("PatchNR: {0:.2f}".format(psnr_patchnr))

plot(
    [test_imgs, fbp.clip(0, 1), recon_epll.clip(0, 1), recon_patchnr.clip(0, 1)],
    ["Ground truth", "Filtered Backprojection", "EPLL", "PatchNR"],
)