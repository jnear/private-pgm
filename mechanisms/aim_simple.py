"""Implementation of AIM: An Adaptive and Iterative Mechanism for DP Synthetic Data.

Note that with the default settings, AIM can take many hours to run.  You can configure
the runtime /utility tradeoff via the max_model_size flag.  We recommend setting it to 1.0
for debugging, but keeping the default value of 80 for any official comparisons to this mechanism.

Note that we assume in this file that the data has been appropriately preprocessed so that there are no large-cardinality categorical attributes.  If there are, we recommend using something like "compress_domain" from mst.py.  Since our paper evaluated already-preprocessed datastes, we did not implement that here for simplicity.
"""

import numpy as np
import itertools
from mbi import (
    Dataset,
    Domain,
    estimation,
    junction_tree,
    LinearMeasurement,
    LinearMeasurement,
)
from mechanism import Mechanism
from collections import defaultdict
from scipy.optimize import bisect
import pandas as pd
from mbi import Factor
import argparse


def powerset(iterable):
    "powerset([1,2,3]) --> (1,) (2,) (3,) (1,2) (1,3) (2,3) (1,2,3)"
    s = list(iterable)
    return itertools.chain.from_iterable(
        itertools.combinations(s, r) for r in range(1, len(s) + 1)
    )


def downward_closure(Ws):
    ans = set()
    for proj in Ws:
        ans.update(powerset(proj))
    return list(sorted(ans, key=len))


def hypothetical_model_size(domain, cliques):
    jtree, _ = junction_tree.make_junction_tree(domain, cliques)
    maximal_cliques = junction_tree.maximal_cliques(jtree)
    cells = sum(domain.size(cl) for cl in maximal_cliques)
    size_mb = cells * 8 / 2**20
    return size_mb


def compile_workload(workload):
    weights = {cl: wt for (cl, wt) in workload}
    workload_cliques = weights.keys()

    def score(cl):
        return sum(
            weights[workload_cl] * len(set(cl) & set(workload_cl))
            for workload_cl in workload_cliques
        )

    return {cl: score(cl) for cl in downward_closure(workload_cliques)}


def filter_candidates(candidates, model, size_limit):
    ans = {}
    free_cliques = downward_closure(model.cliques)
    for cl in candidates:
        cond1 = (
            hypothetical_model_size(model.domain, model.cliques + [cl]) <= size_limit
        )
        cond2 = cl in free_cliques
        if cond1 or cond2:
            ans[cl] = candidates[cl]
    return ans


class AIM(Mechanism):
    def __init__(
        self,
        epsilon,
        delta,
        prng=None,
        rounds=None,
        max_model_size=80,
        max_iters=1000,
        structural_zeros={},
    ):
        super(AIM, self).__init__(epsilon, delta, prng)
        self.rounds = rounds
        self.max_iters = max_iters
        self.max_model_size = max_model_size
        self.structural_zeros = structural_zeros
        self.rho_used = 0

    def worst_approximated(self, candidates, answers, model, rho):
        sigma = np.sqrt(1/(2*rho))
        errors = {}
        sensitivity = {}
        for cl in candidates:
            wgt = candidates[cl]
            x = answers[cl]
            bias = np.sqrt(2 / np.pi) * sigma * model.domain.size(cl)
            xest = model.project(cl).datavector()
            errors[cl] = wgt * (np.linalg.norm(x - xest, 1) - bias)
            sensitivity[cl] = abs(wgt)

        max_sensitivity = max(
            sensitivity.values()
        )  # if all weights are 0, could be a problem

        # TODO: probably easiest to use gumbel noise here
        epsilon = np.sqrt(8*rho)
        return self.exponential_mechanism(errors, epsilon, max_sensitivity)

    def zcdp_gaussian_mech(self, val, sensitivity, rho):
        self.rho_used += rho
        sigma = np.sqrt(sensitivity**2/(2*rho))
        return val + self.prng.normal(0, sigma, val.size)

    def run(self, data, workload, num_synth_rows=None, initial_cliques=None):
        rounds = self.rounds or 16 * len(data.domain)
        candidates = compile_workload(workload)
        answers = {cl: data.project(cl).datavector() for cl in candidates}
        rho_oneway = self.rho * .05
        rho_iterations = self.rho * .95

        if not initial_cliques:
            initial_cliques = [
                cl for cl in candidates if len(cl) == 1
            ]  # use one-way marginals

        oneway = [cl for cl in candidates if len(cl) == 1]

        measurements = []
        rho_oneway_i = rho_oneway / len(oneway)
        for cl in initial_cliques:
            x = data.project(cl).datavector()
            y = self.zcdp_gaussian_mech(x, sensitivity=1, rho=rho_oneway_i)
            std = np.sqrt(1/(2*rho_oneway_i))
            measurements.append(LinearMeasurement(y, cl, stddev=std))

        zeros = self.structural_zeros
        # NOTE: Haven't incorproated structural zeros back yet after refactoring
        model = estimation.mirror_descent(
                data.domain, measurements, iters=self.max_iters, callback_fn=lambda *_: None
        )

        iterations = int(len(workload)/4)
        rho_iters_i = rho_iterations / iterations

        print(f'Running {iterations} iterations...')
        for t in range(iterations):
            size_limit = self.max_model_size * self.rho_used / self.rho
            small_candidates = filter_candidates(candidates, model, size_limit)
            cl = self.worst_approximated(small_candidates, answers, model, rho_iters_i/2)
            print('Measuring Clique', cl)
            n = data.domain.size(cl)
            x = data.project(cl).datavector()
            y = self.zcdp_gaussian_mech(x, sensitivity=1, rho=rho_iters_i/2)
            std = np.sqrt(1/(2*rho_oneway_i))
            measurements.append(LinearMeasurement(y, cl, stddev=std))

            # Warm start potentials from prior round
            # TODO: check if it helps to call maximal_subsets here
            pcliques = list(set(M.clique for M in measurements))
            potentials = model.potentials.expand(pcliques)
            model = estimation.mirror_descent(
                    data.domain, measurements, iters=self.max_iters, potentials=potentials, callback_fn=lambda *_: None
            )
            # print('Selected',cl,'Size',n,'Budget Used',rho_used/self.rho)

        print(f'Total budget used: {self.rho_used}')
        print("Generating Data...")
        model = estimation.mirror_descent(
            data.domain, measurements, iters=self.max_iters
        )
        synth = model.synthetic_data(rows=num_synth_rows)

        return model, synth


def default_params():
    """
    Return default parameters to run this program

    :returns: a dictionary of default parameter settings for each command line argument
    """
    params = {}
    params["dataset"] = "../data/adult.csv"
    params["domain"] = "../data/adult-domain.json"
    params["epsilon"] = 1.0
    params["delta"] = 1e-9
    params["noise"] = "laplace"
    params["max_model_size"] = 80
    params["max_iters"] = 1000
    params["degree"] = 2
    params["num_marginals"] = None
    params["max_cells"] = 10000

    return params


if __name__ == "__main__":

    description = ""
    formatter = argparse.ArgumentDefaultsHelpFormatter
    parser = argparse.ArgumentParser(description=description, formatter_class=formatter)
    parser.add_argument("--dataset", help="dataset to use")
    parser.add_argument("--domain", help="domain to use")
    parser.add_argument("--epsilon", type=float, help="privacy parameter")
    parser.add_argument("--delta", type=float, help="privacy parameter")
    parser.add_argument(
        "--max_model_size", type=float, help="maximum size (in megabytes) of model"
    )
    parser.add_argument("--max_iters", type=int, help="maximum number of iterations")
    parser.add_argument("--degree", type=int, help="degree of marginals in workload")
    parser.add_argument(
        "--num_marginals", type=int, help="number of marginals in workload"
    )
    parser.add_argument(
        "--max_cells",
        type=int,
        help="maximum number of cells for marginals in workload",
    )
    parser.add_argument("--save", type=str, help="path to save synthetic data")

    parser.set_defaults(**default_params())
    args = parser.parse_args()

    data = Dataset.load(args.dataset, args.domain)

    workload = list(itertools.combinations(data.domain, args.degree))
    workload = [cl for cl in workload if data.domain.size(cl) <= args.max_cells]
    if args.num_marginals is not None:
        workload = [
            workload[i]
            for i in prng.choice(len(workload), args.num_marginals, replace=False)
        ]

    workload = [(cl, 1.0) for cl in workload]
    mech = AIM(
        args.epsilon,
        args.delta,
        max_model_size=args.max_model_size,
        max_iters=args.max_iters,
    )
    model, synth = mech.run(data, workload)

    if args.save is not None:
        synth.df.to_csv(args.save, index=False)

    errors = []
    for proj, wgt in workload:
        X = data.project(proj).datavector()
        Y = synth.project(proj).datavector()
        e = 0.5 * wgt * np.linalg.norm(X / X.sum() - Y / Y.sum(), 1)
        errors.append(e)
    print("Average Error: ", np.mean(errors))
