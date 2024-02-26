import torch.nn as nn
import torch
from .gmm import GaussianMixtureModel
from deepinv.utils import patch_extractor

class EPLL(nn.Module):
    r"""
    Defines a prior on the space of patches via Gaussian mixture models. The forward method evaluates the negative log likelihood of the GMM.
    The reconstruction function implements the approximated half-quadratic splitting method as in the original
    paper of Zoran and Weiss.

    :param deepinv.models.GaussianMixtureModel or None GMM: Gaussian mixture defining the distribution on the patch space. 
        None creates a GMM with 200 components of dimension accordingly to the arguments patch_size and channels.
    :param str pretrained_weights or None: Path to pretrained weights of the GMM with file ending .pt. None for no pretrained weights.
    :param int patch_size: patch size.
    :param int channels: number of color channels (e.g. 1 for gray-valued images and 3 for RGB images)
    :param str device: defines device (cpu or cuda)
    """
    def __init__(self,GMM=None,pretrained_weights=None,patch_size=6,channels=1,device='cpu'):
        super(EPLL,self).__init__()
        if GMM is None:
            self.GMM=GaussianMixtureModel(200,patch_size**2*channels,device=device)
        else:
            self.GMM=GMM
        self.patch_size=patch_size
        if pretrained_weights:
            if pretrained_weights[-3:]==".pt":
                weights=torch.load(pretrained_weights)
            else:
                raise NotImplementedError
            self.GMM.load_parameter_dict(weights)
    
    def forward(self,x):
        r"""
        Takes patches and returns the negative log likelihood of the GMM for each patch.

        :param torch.Tensor x: tensor of patches of shape batch_size x number of patches per batch x patch_dimensions
        """
        B,n_patches=x.shape[0:2]
        logpz=self.GMM.negative_log_likelihood(x.view(B*n_patches,-1))
        return logpz.view(B,n_patches)

    def reconstruction(self,y,x_init,sigma_sq,physics,betas=None,batch_size=-1):
        r"""
        Approximated half-quadratic splitting method for image reconstruction as proposed by Zoran and Weiss.
        
        :param torch.Tensor y: tensor of observations. Shape: batch size x ...
        :param torch.Tensor x_init: tensor of initializations. Shape: batch size x channels x height x width
        :param float sigma_sq: squared noise level (acts as regularization parameter)
        :param deepinv.physics.LinearPhysics physics: Forward operator. Has to be linear. Requires physics.A and physics.A_adjoint.
        :param list of floats betas: parameters from the half-quadratic splitting. None uses the standard choice 1/sigma_sq [1,4,8,16,32]
        :param int batch_size: batching the patch estimations for large images. No effect on the output, but a small value reduces the memory consumption
            but might increase the computation time. -1 for considering all patches at once.
        """
        if betas is None:
            # default choice as suggested in Parameswaran et al. "Accelerating GMM-Based Patch Priors for Image Restoration: Three Ingredients for a 100× Speed-Up"
            betas=[beta/sigma_sq for beta in [1.,4.,8.,16.,32.]]
        if y.shape[0]>1:
            # vectorization over a batch of images not implemented....
            return torch.cat([self.reconstruction(y[i:i+1],x_init[i:i+1],betas=betas,batch_size=batch_size) for i in range(y.shape[0])],0)
        x=x_init
        Aty=physics.A_adjoint(y)
        for beta in betas:
            x=self._reconstruction_step(Aty,x,sigma_sq,beta,physics,batch_size)
        return x

    def _reconstruction_step(self,Aty,x,sigma_sq,beta,physics,batch_size):
        # precomputations for GMM with covariance regularization
        self.GMM.set_cov_reg(1./beta)
        N,M=x.shape[2:4]
        total_patch_number=(N-self.patch_size+1)*(M-self.patch_size+1)
        if batch_size==-1 or batch_size>total_patch_number:
            batch_size=total_patch_number
        
        # compute sum P_i^T z and sum P_i^T P_i on the fly with batching
        x_tilde_flattened = torch.zeros_like(x).reshape(-1)
        patch_multiplicities = torch.zeros_like(x).reshape(-1)

        # batching loop over all patches in the image
        ind=0
        while ind<total_patch_number:
            # extract patches
            n_patches=min(batch_size,total_patch_number-ind)
            patch_inds=torch.LongTensor(range(ind,ind+n_patches)).to(x.device)
            patches,linear_inds=patch_extractor(x,n_patches,self.patch_size,position_inds_linear=patch_inds)
            patches=patches.reshape(patches.shape[0]*patches.shape[1],-1)
            linear_inds=linear_inds.reshape(patches.shape[0],-1)

            # Gaussian selection
            k_star=self.GMM.classify(patches,cov_regularization=True)

            # Patch estimation
            estimation_matrices=torch.bmm(self.GMM.get_cov_inv_reg(),self.GMM.get_cov())
            estimation_matrices_k_star=estimation_matrices[k_star]
            patch_estimates=torch.bmm(estimation_matrices_k_star,patches[:,:,None]).reshape(patches.shape[0],patches.shape[1])

            # update on-the-fly parameters
            patch_multiplicities[linear_inds]+=1.
            x_tilde_flattened[linear_inds]+=patch_estimates
            ind=ind+n_patches
        # compute x_tilde
        x_tilde_flattened/=patch_multiplicities

        # Image estimation by CG method
        rhs=Aty+beta*sigma_sq*x_tilde_flattened.view(x.shape)
        op=lambda im:physics.A_adjoint(physics.A(im))+beta*sigma_sq*im
        hat_x=deepinv.optim.utils.conjugate_gradient(op, rhs, max_iter=1e2, tol=1e-5)
        return hat_x