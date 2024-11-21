"""Algorithms for estimating graphical models from marginal-based loss functions.

This module provides a flexible set of optimization algorithms, each sharing the
the same API.  The supported algorithms are:
    1. Mirror Descent [our recommended algorithm]
    2. L-BFGS (using back-belief propagation)
    3. Regularized Dual Averaging
    4. Interior Gradient

Each algorithm can be given an initial set of potentials, or can automatically
intialize the potentials to zero for you.  Any CliqueVector of potentials that
support the cliques of the marginal-based loss function can be used here.
"""


import numpy as np
from mbi import Domain, CliqueVector, Factor, LinearMeasurement
from mbi import marginal_oracles, marginal_loss, synthetic_data
from typing import Callable
import jax
import chex
import attr
import optax

_DEFAULT_CALLBACK = lambda t, loss: print(loss) if t % 50 == 0 else None


# API may change, we'll see
@attr.dataclass(frozen=True)
class GraphicalModel:
    potentials: CliqueVector
    marginals: CliqueVector
    total: chex.Numeric = 1

    def project(self, attrs: tuple[str, ...]) -> Factor:
        try:
            return self.marginals.project(attrs)
        except:
            return marginal_oracles.variable_elimination(
                self.potentials, attrs, self.total
            )

    def synthetic_data(self, rows: int | None = None):
        return synthetic_data.from_marginals(self, rows or self.total)

    @property
    def domain(self):
        return self.potentials.domain

    @property
    def cliques(self):
        return self.potentials.cliques


def minimum_variance_unbiased_total(measurements: list[LinearMeasurement]) -> float:
    # find the minimum variance estimate of the total given the measurements
    estimates, variances = [], []
    for M in measurements:
        y = M.noisy_measurement
        try:
            # TODO: generalize to support any linear measurement that supports total query
            if np.allclose(M.query(y), y):  # query = Identity
                estimates.append(y.sum())
                variances.append(M.stddev**2 * y.size)
        except:
            continue
    estimates, variances = np.array(estimates), np.array(variances)
    if len(estimates) == 0:
        return 1
    else:
        variance = 1.0 / np.sum(1.0 / variances)
        estimate = variance * np.sum(estimates / variances)
        return max(1, estimate)


def _initialize(domain, loss_fn, known_total, potentials):
    if isinstance(loss_fn, list):
        if known_total is None:
            known_total = minimum_variance_unbiased_total(loss_fn)
        loss_fn = marginal_loss.from_linear_measurements(loss_fn)
    elif known_total is None:
        raise ValueError("Must set known_total is giving a custom MarginalLossFn")

    if potentials is None:
        potentials = CliqueVector.zeros(domain, loss_fn.cliques)

    return loss_fn, known_total, potentials


def mirror_descent(
    domain: Domain,
    loss_fn: marginal_loss.MarginalLossFn | list[LinearMeasurement],
    known_total: float | None = None,
    potentials: CliqueVector | None = None,
    marginal_oracle=marginal_oracles.message_passing_new,
    iters: int = 1000,
    stepsize: float | None = None,
    callback_fn: Callable[[CliqueVector], None] = lambda _: None,
):
    """Optimization using the Mirror Descent algorithm.

    This is a first-order proximal optimization algorithm for solving
    a (possibly nonsmooth) convex optimization problem over the marginal polytope.
    This is an  implementation of Algorithm 1 from the paper
    ["Graphical-model based estimation and inference for differential privacy"]
    (https://arxiv.org/pdf/1901.09136).  If stepsize is not provided, this algorithm
    uses a line search to automatically choose appropriate step sizes that satisfy
    the Armijo condition.

    Args:
        domain: The domain over which the model should be defined.
        loss_fn: A MarginalLossFn or a list of Linear Measurements.
        known_total: The known or estimated number of records in the data.
        potentials: The initial potentials.  Must be defind over a set of cliques
            that supports the cliques in the loss_fn.
        marginal_oracle: The function to use to compute marginals from potentials.
        iters: The maximum number of optimization iterations.
        stepsize: The step size for the optimization.  If not provided, this algorithm
            will use a line search to automatically choose appropriate step sizes.
        callback_fn: A function to call at each iteration with the iteration number

    Returns:
        A GraphicalModel object with the estimated potentials and marginals.
    """
    loss_fn, known_total, potentials = _initialize(
        domain, loss_fn, known_total, potentials
    )


    @jax.jit
    def update(theta, alpha):

        mu = marginal_oracle(theta, known_total)
        loss, dL = jax.value_and_grad(loss_fn)(mu)

        theta2 = theta - alpha * dL
        if stepsize is not None:
            return theta2, loss, alpha, mu

        mu2 = marginal_oracle(theta2, known_total)
        loss2 = loss_fn(mu2)

        sufficient_decrease = loss - loss2 >= 0.5 * alpha * dL.dot(mu - mu2)
        alpha = jax.lax.select(sufficient_decrease, alpha, 0.5 * alpha)
        theta = jax.lax.cond(sufficient_decrease, lambda: theta2, lambda: theta)
        loss = jax.lax.select(sufficient_decrease, loss2, loss)

        return theta, loss, alpha, mu

    alpha = 2.0 if stepsize is None else stepsize 
    for t in range(iters):
        potentials, loss, alpha, mu = update(potentials, alpha)
        callback_fn(mu)

    marginals = marginal_oracle(potentials, known_total)
    return GraphicalModel(potentials, marginals, known_total)


def lbfgs(
    domain: Domain,
    loss_fn: marginal_loss.MarginalLossFn | list[LinearMeasurement],
    known_total: float | None = None,
    potentials: CliqueVector | None = None,
    marginal_oracle=marginal_oracles.message_passing_new,
    iters: int = 1000,
    callback_fn: Callable[[CliqueVector], None] = lambda _: None,
):
    """Gradient-based optimization on the potentials (theta) via L-BFGS.

    This optimizer works by calculating the gradients with respect to the
    potentials by back-propagting through the marginal inference oracle.

    This is a standard approach for fitting the parameters of a graphical model
    without noise (i.e., when you know the exact marginals).  In this case,
    the loss function with respect to theta is convex, and therefore this approach
    enjoys convergence guarantees.  With generic marginal loss functions that arise
    for instance iwth noise marginals, the loss function is typically convex with
    respect to mu, but not with respect to theta.  Therefore, this optimizer is not
    guaranteed to converge to the global optimum in all cases.  In practice, it
    tends to work well in these settings despite non-convexities.  This approach
    appeared in the paper ["Learning Graphical Model Parameters with Approximate
    Marginal Inference"](https://arxiv.org/abs/1301.3193).

    Args:
      domain: The domain over which the model should be defined.
      loss_fn: A MarginalLossFn or a list of Linear Measurements.
      known_total: The known or estimated number of records in the data.
        If loss_fn is provided as a list of LinearMeasurements, this argument
        is optional.  Otherwise, it is required.
      potentials: The initial potentials.  Must be defind over a set of cliques
        that supports the cliques in the loss_fn.
      marginal_oracle: The function to use to compute marginals from potentials.
      iters: The maximum number of optimization iterations.
      callback_fn
    """
    loss_fn, known_total, potentials = _initialize(
        domain, loss_fn, known_total, potentials
    )

    # TODO: confirm that potentials.cliques supports loss_fn.cliques.

    theta_loss = lambda theta: loss_fn(marginal_oracle(theta, known_total))
    theta_loss_and_grad = jax.value_and_grad(theta_loss)

    @jax.jit
    def update(theta, opt_state):
        loss, grad = theta_loss_and_grad(theta)
        updates, opt_state = optimizer.update(
            grad, opt_state, theta, value=loss, grad=grad, value_fn=theta_loss
        )
        return optax.apply_updates(theta, updates), opt_state, loss

    optimizer = optax.lbfgs(
        memory_size=1, linesearch=optax.scale_by_backtracking_linesearch(128)
    )
    state = optimizer.init(potentials)
    for t in range(iters):
        potentials, state, loss = update(potentials, state)
        #callback_fn(t, loss)

    marginals = marginal_oracle(potentials, known_total)
    return GraphicalModel(potentials, marginals, known_total)


def mle_from_marginals(marginals: CliqueVector, known_total: float) -> GraphicalModel:
    """Compute the MLE Graphical Model from the marginals.

    Args:
        marginals: The marginal probabilities.
        known_total: The known or estimated number of records in the data.

    Returns:
        A GraphicalModel object with the final potentials and marginals.
    """
    # TODO: wire in something here (custom vjp) for more efficient grad
    negative_log_likelihood = lambda mu: -marginals.dot(mu.log())
    loss_fn = marginal_loss.MarginalLossFn(marginals.cliques, negative_log_likelihood)
    return lbfgs(marginals.domain, loss_fn, known_total, callback_fn=lambda *_: None, iters=150)


def dual_averaging(
    domain: Domain,
    loss_fn: marginal_loss.MarginalLossFn | list[LinearMeasurement],
    lipschitz: float,
    known_total: float | None = None,
    potentials: CliqueVector | None = None,
    marginal_oracle=marginal_oracles.message_passing_new,
    iters: int = 1000,
    callback_fn: Callable[[CliqueVector], None] = lambda _: None,
) -> GraphicalModel:
    """Optimization using the Regularized Dual Averaging (RDA) algorithm.

    RDA is an accelerated proximal algorithm for solving a smooth convex optimization
    problem over the marginal polytope.  This algorithm requires knowledge of
    the Lipschitz constant of the gradient of the loss function.

    Args:
        domain: The domain over which the model should be defined.
        loss_fn: A MarginalLossFn or a list of Linear Measurements.
        lipschitz: The Lipschitz constant of the gradient of the loss function.
        known_total: The known or estimated number of records in the data.
        potentials: The initial potentials.  Must be defind over a set of cliques
            that supports the cliques in the loss_fn.
        marginal_oracle: The function to use to compute marginals from potentials.
        iters: The maximum number of optimization iterations.
        callback_fn: A function to call with intermediate solution at each iteration.

    Returns:
        A GraphicalModel object with the final potentials and marginals.
    """
    loss_fn, known_total, potentials = _initialize(
        domain, loss_fn, known_total, potentials
    )

    beta = 0
    L = lipschitz

    @jax.jit
    def update(w, v, gbar, c):
        u = (1 - c) * w + c * v
        g = jax.grad(loss_fn)(u)
        gbar = (1 - c) * gbar + c * g
        theta = -t * (t + 1) / (4 * L + beta) / known_total * gbar
        v = marginal_oracle(theta, known_total)
        w = (1 - c) * w + c * v
        return w, v, gbar

    w = v = marginal_oracle(potentials, known_total)
    gbar = CliqueVector.zeros(domain, loss_fn.cliques)
    for t in range(1, iters + 1):
        c = 2.0 / (t + 1)
        w, v, gbar = update(w, v, gbar, c)
        callback_fn(w)

    return w # mle_from_marginals(w, known_total)


def interior_gradient(
    domain: Domain,
    loss_fn: marginal_loss.MarginalLossFn | list[LinearMeasurement],
    lipschitz: float | None = None,
    known_total: float | None = None,
    potentials: CliqueVector | None = None,
    marginal_oracle=marginal_oracles.message_passing_new,
    iters: int = 1000,
    stepsize: float | None = None,
    callback_fn: Callable[[CliqueVector], None] = lambda _: None,
):
    """Optimization using the Interior Point Gradient Descent algorithm.

    Interior Gradient is an accelerated proximal algorithm for solving a smooth
    convex optimization problem over the marginal polytope.  This algorithm
    requires knowledge of the Lipschitz constant of the gradient of the loss function.
    This algorithm is based on the paper titled
    ["Interior Gradient and Proximal Methods for Convex and Conic Optimization"](https://epubs.siam.org/doi/abs/10.1137/S1052623403427823?journalCode=sjope8).

    Args:
        domain: The domain over which the model should be defined.
        loss_fn: A MarginalLossFn or a list of Linear Measurements.
        lipschitz: The Lipschitz constant of the gradient of the loss function.
        known_total: The known or estimated number of records in the data.
        potentials: The initial potentials.  Must be defind over a set of cliques
            that supports the cliques in the loss_fn.
        marginal_oracle: The function to use to compute marginals from potentials.
        iters: The maximum number of optimization iterations.
        callback_fn: A function to call at each iteration with the iteration number

    Returns:
        A GraphicalModel object with the optimized potentials and marginals.
    """
    loss_fn, known_total, potentials = _initialize(
        domain, loss_fn, known_total, potentials
    )

    # Algorithm parameters
    c = 1
    sigma = 1
    l = sigma / lipschitz

    @jax.jit
    def update(theta, c, x, y, z):
        a = (((c * l) ** 2 + 4 * c * l) ** 0.5 - l * c) / 2
        y = (1 - a) * x + a * z
        c = c * (1 - a)
        g = jax.grad(loss_fn)(y)
        theta = theta - a / c / known_total * g
        z = marginal_oracle(theta, known_total)
        x = (1 - a) * x + a * z
        return theta, c, x, y, z

    x = y = z = marginal_oracle(potentials, known_total)
    gbar = CliqueVector.zeros(domain, loss_fn.cliques)
    theta = potentials
    for t in range(1, iters + 1):
        theta, c, x, y, z = update(theta, c, x, y, z)
        callback_fn(x)

    return  x #mle_from_marginals(x, known_total)
