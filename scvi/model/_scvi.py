import logging
from typing import List, Literal, Optional

import torch
import numpy as np
from anndata import AnnData
import scanpy as sc
from scvi import REGISTRY_KEYS
from scvi._types import MinifiedDataType
from scvi.data import AnnDataManager
from scvi.data._constants import _ADATA_MINIFY_TYPE_UNS_KEY, ADATA_MINIFY_TYPE
from scvi.data._utils import _get_adata_minify_type
from scvi.data.fields import (
    BaseAnnDataField,
    CategoricalJointObsField,
    CategoricalObsField,
    LayerField,
    NumericalJointObsField,
    NumericalObsField,
    ObsmField,
    StringUnsField,
)
from scvi.model._utils import _init_library_size
from scvi.model.base import UnsupervisedTrainingMixin
from scvi.model.utils import get_minified_adata_scrna
from scvi.module import VAE
from scvi.utils import setup_anndata_dsp
from sklearn.decomposition import PCA
from k_means_constrained import KMeansConstrained
from .base import ArchesMixin, BaseMinifiedModeModelClass, RNASeqMixin, VAEMixin

_SCVI_LATENT_QZM = "_scvi_latent_qzm"
_SCVI_LATENT_QZV = "_scvi_latent_qzv"
_SCVI_OBSERVED_LIB_SIZE = "_scvi_observed_lib_size"

logger = logging.getLogger(__name__)


class SCVI(
    RNASeqMixin,
    VAEMixin,
    ArchesMixin,
    UnsupervisedTrainingMixin,
    BaseMinifiedModeModelClass,
):
    """single-cell Variational Inference :cite:p:`Lopez18`.

    Parameters
    ----------
    adata
        AnnData object that has been registered via :meth:`~scvi.model.SCVI.setup_anndata`.
    n_hidden
        Number of nodes per hidden layer.
    n_latent
        Dimensionality of the latent space.
    n_layers
        Number of hidden layers used for encoder and decoder NNs.
    dropout_rate
        Dropout rate for neural networks.
    dispersion
        One of the following:

        * ``'gene'`` - dispersion parameter of NB is constant per gene across cells
        * ``'gene-batch'`` - dispersion can differ between different batches
        * ``'gene-label'`` - dispersion can differ between different labels
        * ``'gene-cell'`` - dispersion can differ for every gene in every cell
    gene_likelihood
        One of:

        * ``'nb'`` - Negative binomial distribution
        * ``'zinb'`` - Zero-inflated negative binomial distribution
        * ``'poisson'`` - Poisson distribution
    latent_distribution
        One of:

        * ``'normal'`` - Normal distribution
        * ``'ln'`` - Logistic normal distribution (Normal(0, I) transformed by softmax)
    **model_kwargs
        Keyword args for :class:`~scvi.module.VAE`

    Examples
    --------
    >>> adata = anndata.read_h5ad(path_to_anndata)
    >>> scvi.model.SCVI.setup_anndata(adata, batch_key="batch")
    >>> vae = scvi.model.SCVI(adata)
    >>> vae.train()
    >>> adata.obsm["X_scVI"] = vae.get_latent_representation()
    >>> adata.obsm["X_normalized_scVI"] = vae.get_normalized_expression()

    Notes
    -----
    See further usage examples in the following tutorials:

    1. :doc:`/tutorials/notebooks/quick_start/api_overview`
    2. :doc:`/tutorials/notebooks/scrna/harmonization`
    3. :doc:`/tutorials/notebooks/scrna/scarches_scvi_tools`
    4. :doc:`/tutorials/notebooks/scrna/scvi_in_R`
    """

    _module_cls = VAE

    def __init__(
        self,
        adata: AnnData,
        n_dims =  None,
        conv_dims = None,
        post_weight = 1,
        pca_max_dim: int = 200,
        n_hidden: int = 128,
        n_hvg: int = 5000,
        n_clusters: int = 10,
        n_prior_clusters: int = 2,
        n_pcs: int = 50,
        n_z1: int = 10,
        n_delta: int= 10,
        n_levels: int = 2,
        n_latent: int = 30,
        prior_type_ = "NORMAL",
        n_first: int = 500,
        n_layers: int = 1,
        dropout_rate: float = 0.1,
        dispersion: Literal["gene", "gene-batch", "gene-label", "gene-cell"] = "gene",
        gene_likelihood: Literal["zinb", "nb", "poisson"] = "zinb",
        latent_distribution: Literal["normal", "ln"] = "normal",
        type_ = "NVAE",
        **model_kwargs,
    ):
        super().__init__(adata)
        print(type_)
        n_cats_per_cov = (
            self.adata_manager.get_state_registry(
                REGISTRY_KEYS.CAT_COVS_KEY
            ).n_cats_per_key
            if REGISTRY_KEYS.CAT_COVS_KEY in self.adata_manager.data_registry
            else None
        )
        n_batch = self.summary_stats.n_batch
        use_size_factor_key = (
            REGISTRY_KEYS.SIZE_FACTOR_KEY in self.adata_manager.data_registry
        )
        library_log_means, library_log_vars = None, None
        if not use_size_factor_key and self.minified_data_type is None:
            library_log_means, library_log_vars = _init_library_size(
                self.adata_manager, n_batch
            )
        #sc.pp.highly_variable_genes(adata, n_top_genes=n_hvg, flavor="cell_ranger", batch_key="batch",subset = False)
        self.highly_variable = adata.var["highly_variable"]
        self.pca_M = None # n_pcs*n_clusters X n_genes
        self.pca_means = None
        self.n_levels = n_levels
        self.conv_dims = conv_dims
        if type_ == "NVAE" or type_ == "NVAE_PCA":
            self.hkmkb_pro(adata,n_clusters,n_first,n_levels)
        self.module = self._module_cls(
            n_dims = n_dims,
            conv_dims = self.conv_dims,
            n_input=self.summary_stats.n_vars,
            pca_M = self.pca_M,
            pca_means = self.pca_means,
            pca_max_dim = pca_max_dim,
            post_weight= post_weight,
            highly_variable = self.highly_variable,
            n_batch=n_batch,
            n_labels=self.summary_stats.n_labels,
            n_continuous_cov=self.summary_stats.get("n_extra_continuous_covs", 0),
            n_cats_per_cov=n_cats_per_cov,
            n_hidden=n_hidden,
            n_levels=n_levels,
            n_latent=n_latent,
            prior_type_=prior_type_,
            n_clusters=n_prior_clusters,
            n_z1 = n_z1,
            n_delta=n_delta,
            n_layers=n_layers,
            dropout_rate=dropout_rate,
            dispersion=dispersion,
            gene_likelihood=gene_likelihood,
            latent_distribution=latent_distribution,
            use_size_factor_key=use_size_factor_key,
            library_log_means=library_log_means,
            library_log_vars=library_log_vars,
            type_ = type_,
            **model_kwargs,
        )
        self.module.minified_data_type = self.minified_data_type
        self._model_summary_string = (
            "SCVI Model with the following params: \nn_hidden: {}, n_latent: {}, n_levels: {},  n_layers: {}, dropout_rate: "
            "{}, dispersion: {}, gene_likelihood: {}, latent_distribution: {}, pca_max_dim: {}, prior_type_: {}, type_: {}, "
            "n_prior_clusters: {}, n_pca_clusters: {},\n"
            "conv_dims: {},\n n_dims: {}"
        ).format(
            n_hidden,
            n_latent,
            n_levels,
            n_layers,
            dropout_rate,
            dispersion,
            gene_likelihood,
            latent_distribution,
            pca_max_dim,
            prior_type_,
            type_,
            n_prior_clusters,
            n_clusters,
            self.conv_dims,
            self.module.n_dims,
        )
        self.init_params_ = self._get_init_params(locals())

    @classmethod
    @setup_anndata_dsp.dedent
    def setup_anndata(
        cls,
        adata: AnnData,
        layer: Optional[str] = None,
        batch_key: Optional[str] = None,
        labels_key: Optional[str] = None,
        size_factor_key: Optional[str] = None,
        categorical_covariate_keys: Optional[List[str]] = None,
        continuous_covariate_keys: Optional[List[str]] = None,
        **kwargs,
    ):

        """%(summary)s.

        Parameters
        ----------
        %(param_adata)s
        %(param_layer)s
        %(param_batch_key)s
        %(param_labels_key)s
        %(param_size_factor_key)s
        %(param_cat_cov_keys)s
        %(param_cont_cov_keys)s
        """
        setup_method_args = cls._get_setup_method_args(**locals())
        anndata_fields = [
            LayerField(REGISTRY_KEYS.X_KEY, layer, is_count_data=True),
            CategoricalObsField(REGISTRY_KEYS.BATCH_KEY, batch_key),
            CategoricalObsField(REGISTRY_KEYS.LABELS_KEY, labels_key),
            NumericalObsField(
                REGISTRY_KEYS.SIZE_FACTOR_KEY, size_factor_key, required=False
            ),
            CategoricalJointObsField(
                REGISTRY_KEYS.CAT_COVS_KEY, categorical_covariate_keys
            ),
            NumericalJointObsField(
                REGISTRY_KEYS.CONT_COVS_KEY, continuous_covariate_keys
            ),
        ]
        # register new fields if the adata is minified
        adata_minify_type = _get_adata_minify_type(adata)
        if adata_minify_type is not None:
            anndata_fields += cls._get_fields_for_adata_minification(adata_minify_type)
        adata_manager = AnnDataManager(
            fields=anndata_fields, setup_method_args=setup_method_args
        )
        adata_manager.register_fields(adata, **kwargs)
        cls.register_manager(adata_manager)

    @staticmethod
    def _get_fields_for_adata_minification(
        minified_data_type: MinifiedDataType,
    ) -> List[BaseAnnDataField]:
        """Return the anndata fields required for adata minification of the given minified_data_type."""
        if minified_data_type == ADATA_MINIFY_TYPE.LATENT_POSTERIOR:
            fields = [
                #ObsmField(
                #    REGISTRY_KEYS.LATENT_QZM_KEY,
                 #   _SCVI_LATENT_QZM,
                #),'''
                ObsmField(
                    REGISTRY_KEYS.LATENT_QZV_KEY,
                    _SCVI_LATENT_QZV,
                ),
                NumericalObsField(
                    REGISTRY_KEYS.OBSERVED_LIB_SIZE,
                    _SCVI_OBSERVED_LIB_SIZE,
                ),
            ]
        else:
            raise NotImplementedError(f"Unknown MinifiedDataType: {minified_data_type}")
        fields.append(
            StringUnsField(
                REGISTRY_KEYS.MINIFY_TYPE_KEY,
                _ADATA_MINIFY_TYPE_UNS_KEY,
            ),
        )
        return fields

    def minify_adata(
        self,
        minified_data_type: MinifiedDataType = ADATA_MINIFY_TYPE.LATENT_POSTERIOR,
        use_latent_qzm_key: str = "X_latent_qzm",
        use_latent_qzv_key: str = "X_latent_qzv",
    ) -> None:
        """Minifies the model's adata.

        Minifies the adata, and registers new anndata fields: latent qzm, latent qzv, adata uns
        containing minified-adata type, and library size.
        This also sets the appropriate property on the module to indicate that the adata is minified.

        Parameters
        ----------
        minified_data_type
            How to minify the data. Currently only supports `latent_posterior_parameters`.
            If minified_data_type == `latent_posterior_parameters`:

            * the original count data is removed (`adata.X`, adata.raw, and any layers)
            * the parameters of the latent representation of the original data is stored
            * everything else is left untouched
        use_latent_qzm_key
            Key to use in `adata.obsm` where the latent qzm params are stored
        use_latent_qzv_key
            Key to use in `adata.obsm` where the latent qzv params are stored

        Notes
        -----
        The modification is not done inplace -- instead the model is assigned a new (minified)
        version of the adata.
        """
        # TODO(adamgayoso): Add support for a scenario where we want to cache the latent posterior
        # without removing the original counts.
        if minified_data_type != ADATA_MINIFY_TYPE.LATENT_POSTERIOR:
            raise NotImplementedError(f"Unknown MinifiedDataType: {minified_data_type}")

        if self.module.use_observed_lib_size is False:
            raise ValueError(
                "Cannot minify the data if `use_observed_lib_size` is False"
            )

        minified_adata = get_minified_adata_scrna(self.adata, minified_data_type)
        #minified_adata.obsm[_SCVI_LATENT_QZM] = self.adata.obsm[use_latent_qzm_key]
        minified_adata.obsm[_SCVI_LATENT_QZV] = self.adata.obsm[use_latent_qzv_key]
        counts = self.adata_manager.get_from_registry(REGISTRY_KEYS.X_KEY)
        minified_adata.obs[_SCVI_OBSERVED_LIB_SIZE] = np.squeeze(
            np.asarray(counts.sum(axis=1))
        )
        self._update_adata_and_manager_post_minification(
            minified_adata, minified_data_type
        )
        self.module.minified_data_type = minified_data_type
    def hkmkb(
        self,
        adata: AnnData,
        n_clusters: int,
        n_pcs: int,
    ):
        matrix_list = []
        num_genes = np.sum(self.highly_variable)
        matrix = np.asarray(adata.X)[:,self.highly_variable]
        print("Starting Clustering")
        clf = KMeansConstrained(n_clusters=n_clusters,
            size_min=num_genes/n_clusters,
            random_state=0)
        clf.fit_predict(matrix.T)
        print("Clustering Done")
        labels = clf.labels_
        #print(matrix)
        for i in range(n_clusters):
            matrix_copy = matrix.copy()
            matrix_copy[:,labels!=i] = 0
            print(matrix_copy)
            pca = PCA(n_components=n_pcs)
            pca.fit(matrix_copy)
            pca_matrix = pca.components_
            pca_matrix[:,labels!=i] = 0
            matrix_list.append(pca_matrix)
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        #print(matrix_list)
        self.means = torch.from_numpy(np.mean(matrix,axis=0)).to(device)
        self.M = torch.tensor(np.concatenate(matrix_list,axis=0)).to(device)
        #print(self.M)
        '''matrix = np.asarray(adata.X.todense())
        pca = PCA(n_components=n_pcs)
        pca.fit(matrix.copy())
        #tmp = pca.fit_transform(matrix.copy())
        self.M = pca
        print(self.M.transform(matrix.copy()))'''

    def hkmkb_pro(
        self,
        adata: AnnData,
        n_clusters: int,
        n_first,
        n_levels: int,
    ):

        n_last = np.sum(self.highly_variable)

        if n_levels > 1 and self.conv_dims is None:
            self.conv_dims = [(n_last*(n_levels - i - 1) + n_first*i)//(n_levels - 1) for i in range(n_levels)]
        matrix = np.asarray(adata.X)[:,self.highly_variable]
        print(f"conv_dims = {self.conv_dims}")

        pca_M, pca_means = [None,], [None,]

        for j in range(1,n_levels):
            print(f"Clustering + PCA {j}")
            clf = KMeansConstrained(n_clusters=n_clusters, size_min=self.conv_dims[j-1]//n_clusters, random_state=0)
            clf.fit_predict(matrix.T)
            labels = clf.labels_
            matrix_list = []
            pca_dims = [(self.conv_dims[j] // n_clusters) + (i < self.conv_dims[j] % n_clusters) for i in range(n_clusters)]
            for i in range(n_clusters):
                matrix_copy = matrix.copy()
                matrix_copy[:,labels!=i] = 0
                # print(matrix_copy)
                pca = PCA(n_components=pca_dims[i])
                pca.fit(matrix_copy)
                pca_matrix = pca.components_
                pca_matrix[:,labels!=i] = 0
                matrix_list.append(pca_matrix)

            mean = np.mean(matrix,axis=0)
            M = np.concatenate(matrix_list,axis=0)
            matrix = (matrix - mean) @ M.T

            mean = torch.from_numpy(mean)
            M = torch.tensor(M)

            pca_means.append(mean)
            pca_M.append(M)
            print("Done")
        self.pca_M = pca_M
        self.pca_means = pca_means


        # [(n // k) + (i < n % k) for i in range(k)]






