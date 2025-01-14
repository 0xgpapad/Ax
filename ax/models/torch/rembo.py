#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from typing import Any, Callable, Dict, List, Optional, Tuple

import torch
from ax.core.search_space import SearchSpaceDigest
from ax.core.types import TCandidateMetadata, TConfig, TGenMetadata
from ax.models.torch.botorch import BotorchModel
from ax.models.torch_base import TorchModel
from ax.utils.common.docutils import copy_doc
from torch import Tensor


class REMBO(BotorchModel):
    """Implements REMBO (Bayesian optimization in a linear subspace).

    The (D x d) projection matrix A must be provided, and must be that used for
    the initialization. In the original REMBO paper A ~ N(0, 1). Box bounds
    in the low-d space must also be provided, which in the REMBO paper should
    be [(-sqrt(d), sqrt(d)]^d.

    Function evaluations happen in the high-D space, and so the arms on the
    experiment will also be tracked in the high-D space. This class maintains
    a list of points in the low-d spac that have been launched, so we can match
    arms in high-D space back to their low-d point on update.

    Args:
        A: (D x d) projection matrix.
        initial_X_d: Points in low-d space for initial data.
        bounds_d: Box bounds in the low-d space.
        kwargs: kwargs for BotorchModel init
    """

    def __init__(
        self,
        A: Tensor,
        initial_X_d: Tensor,
        bounds_d: List[Tuple[float, float]],
        **kwargs: Any,
    ) -> None:
        self.A = A
        self._pinvA = torch.pinverse(A)  # compute pseudo inverse once and cache it
        # Projected points in low-d space generated in the optimization
        self.X_d = list(initial_X_d)
        self.X_d_gen = []  # Projected points that were generated by this model
        self.bounds_d = bounds_d
        self.num_outputs = 0
        super().__init__(**kwargs)

    @copy_doc(TorchModel.fit)
    def fit(
        self,
        Xs: List[Tensor],
        Ys: List[Tensor],
        Yvars: List[Tensor],
        search_space_digest: SearchSpaceDigest,
        metric_names: List[str],
        candidate_metadata: Optional[List[List[TCandidateMetadata]]] = None,
    ) -> None:
        assert len(search_space_digest.task_features) == 0
        assert len(search_space_digest.fidelity_features) == 0
        for b in search_space_digest.bounds:
            # REMBO assumes the input space is [-1, 1]^D
            assert b == (-1, 1)
        self.num_outputs = len(Xs)
        # For convenience for now, assume X for all outcomes the same
        X_D = _get_single_X(Xs)
        X_d = self.project_down(X_D)
        # Fit model in low-d space (adjusted to [0, 1]^d)
        super().fit(
            Xs=[self.to_01(X_d)] * self.num_outputs,
            Ys=Ys,
            Yvars=Yvars,
            search_space_digest=SearchSpaceDigest(
                feature_names=[f"x{i}" for i in range(self.A.shape[1])],
                bounds=[(0.0, 1.0)] * len(self.bounds_d),
                task_features=search_space_digest.task_features,
                fidelity_features=search_space_digest.fidelity_features,
            ),
            metric_names=metric_names,
            candidate_metadata=candidate_metadata,
        )

    def to_01(self, X_d: Tensor) -> Tensor:
        """Map points from bounds_d to [0, 1].

        Args:
            X_d: Tensor in bounds_d

        Returns: Tensor in [0, 1].
        """
        X_d01 = X_d.clone()
        for i, (lb, ub) in enumerate(self.bounds_d):
            X_d01[:, i] = (X_d01[:, i] - lb) / (ub - lb)
        return X_d01

    def from_01(self, X_d01: Tensor) -> Tensor:
        """Map points from [0, 1] to bounds_d.

        Args:
            X_d01: Tensor in [0, 1]

        Returns: Tensor in bounds_d.
        """
        X_d = X_d01.clone()
        for i, (lb, ub) in enumerate(self.bounds_d):
            X_d[:, i] = X_d[:, i] * (ub - lb) + lb
        return X_d

    def project_down(self, X_D: Tensor) -> Tensor:
        """Map points in the high-D space to the low-d space by looking them
        up in self.X_d.

        We assume that X_D = self.project_up(self.X_d), except possibly with
        rows shuffled. If a value in X_d cannot be found for each row in X_D,
        an error will be raised.

        This is quite fast relative to model fitting, so we do it in O(n^2)
        time and don't worry about it.

        Args:
            X_D: Tensor in high-D space.

        Returns:
            X_d: Tensor in low-d space.
        """
        X_d = []
        unmatched = list(range(len(self.X_d)))
        for x_D in X_D:
            idx_match = None
            for d_idx in unmatched:
                if torch.allclose(x_D, self.project_up(self.X_d[d_idx])):
                    idx_match = d_idx
                    break
            if idx_match is not None:
                X_d.append(self.X_d[idx_match])
                unmatched.remove(idx_match)
            else:
                raise ValueError("Failed to project X down.")
        return torch.stack(X_d)

    def project_up(self, X: Tensor) -> Tensor:
        """Project to high-dimensional space."""
        Z = torch.t(self.A @ torch.t(X))
        Z = torch.clamp(Z, min=-1, max=1)
        return Z

    @copy_doc(TorchModel.predict)
    def predict(self, X: Tensor) -> Tuple[Tensor, Tensor]:
        # Suports preditions in both low-d and high-D space, depending on shape
        # of X. For high-D, predictions are restricted to within the linear
        # embedding, so can project down with pseudoinverse.
        if X.shape[1] == self.A.shape[1]:
            # X is in low-d space
            X_d = X  # pragma: no cover
        else:
            # Project down to low-d space
            X_d = X @ torch.t(self._pinvA)
            # Project X_d back up to verify X was within linear embedding
            if not torch.allclose(X, X_d @ torch.t(self.A)):
                raise NotImplementedError(
                    "Predictions outside the linear embedding not supported."
                )
        return super().predict(X=self.to_01(X_d))

    @copy_doc(TorchModel.gen)
    def gen(
        self,
        n: int,
        bounds: List[Tuple[float, float]],
        objective_weights: Tensor,
        outcome_constraints: Optional[Tuple[Tensor, Tensor]] = None,
        linear_constraints: Optional[Tuple[Tensor, Tensor]] = None,
        fixed_features: Optional[Dict[int, float]] = None,
        pending_observations: Optional[List[Tensor]] = None,
        model_gen_options: Optional[TConfig] = None,
        rounding_func: Optional[Callable[[Tensor], Tensor]] = None,
        target_fidelities: Optional[Dict[int, float]] = None,
    ) -> Tuple[Tensor, Tensor, TGenMetadata, Optional[List[TCandidateMetadata]]]:
        for b in bounds:
            assert b == (-1, 1)
        # The following can be easily handled in the future when needed
        assert linear_constraints is None
        assert fixed_features is None
        assert pending_observations is None
        # Do gen in the low-dimensional space and project up
        Xopt_01, w, _gen_metadata, _candidate_metadata = super().gen(
            n=n,
            bounds=[(0.0, 1.0)] * len(self.bounds_d),
            objective_weights=objective_weights,
            outcome_constraints=outcome_constraints,
            model_gen_options=model_gen_options,
        )
        Xopt = self.from_01(Xopt_01)
        self.X_d.extend([x.clone() for x in Xopt])
        self.X_d_gen.extend([x.clone() for x in Xopt])
        return self.project_up(Xopt), w, {}, None

    @copy_doc(TorchModel.best_point)
    def best_point(
        self,
        bounds: List[Tuple[float, float]],
        objective_weights: Tensor,
        outcome_constraints: Optional[Tuple[Tensor, Tensor]] = None,
        linear_constraints: Optional[Tuple[Tensor, Tensor]] = None,
        fixed_features: Optional[Dict[int, float]] = None,
        model_gen_options: Optional[TConfig] = None,
        target_fidelities: Optional[Dict[int, float]] = None,
    ) -> Optional[Tensor]:
        for b in bounds:
            assert b == (-1, 1)
        assert linear_constraints is None
        assert fixed_features is None
        x_best = super().best_point(
            bounds=self.bounds_d,
            objective_weights=objective_weights,
            outcome_constraints=outcome_constraints,
            model_gen_options=model_gen_options,
        )
        if x_best is not None:
            x_best = self.project_up(self.from_01(x_best.unsqueeze(0))).squeeze(0)
        return x_best

    @copy_doc(TorchModel.cross_validate)
    def cross_validate(
        self,
        Xs_train: List[Tensor],
        Ys_train: List[Tensor],
        Yvars_train: List[Tensor],
        X_test: Tensor,
        **kwargs: Any,
    ) -> Tuple[Tensor, Tensor]:
        X_D = _get_single_X(Xs_train)
        X_train_d = self.project_down(X_D)
        X_test_d = self.project_down(X_test)
        return super().cross_validate(
            Xs_train=[self.to_01(X_train_d)] * self.num_outputs,
            Ys_train=Ys_train,
            Yvars_train=Yvars_train,
            X_test=self.to_01(X_test_d),
        )

    @copy_doc(TorchModel.update)
    def update(
        self,
        Xs: List[Tensor],
        Ys: List[Tensor],
        Yvars: List[Tensor],
        candidate_metadata: Optional[List[List[TCandidateMetadata]]] = None,
        **kwargs: Any,
    ) -> None:
        X_D = _get_single_X(Xs)
        X_d = self.project_down(X_D)
        super().update(
            Xs=[self.to_01(X_d)] * self.num_outputs,
            Ys=Ys,
            Yvars=Yvars,
            candidate_metadata=candidate_metadata,
        )


def _get_single_X(Xs: List[Tensor]) -> Tensor:
    """Verify all X are identical, and return one.

    Args:
        Xs: A list of X tensors

    Returns: Xs[0], after verifying they are all identical.
    """
    X = Xs[0]
    for i in range(1, len(Xs)):
        assert torch.allclose(X, Xs[i])
    return X
