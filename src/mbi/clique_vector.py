import numpy as np
from mbi import Domain, Factor
import jax
import functools

@functools.partial(
    jax.tree_util.register_dataclass,
    meta_fields=[],
    data_fields=['dictionary']
)
class CliqueVector(dict):
    """ This is a convenience class for simplifying arithmetic over the 
        concatenated vector of marginals and potentials.

        These vectors are represented as a dictionary mapping cliques (subsets of attributes)
        to marginals/potentials (Factor objects)
    """
    def __init__(self, dictionary):
        self.dictionary = dictionary
        dict.__init__(self, dictionary)

    @staticmethod
    def zeros(domain, cliques):
        return CliqueVector({cl: Factor.zeros(domain.project(cl)) for cl in cliques})

    @staticmethod
    def ones(domain, cliques):
        return CliqueVector({cl: Factor.ones(domain.project(cl)) for cl in cliques})

    @staticmethod
    def uniform(domain, cliques):
        return CliqueVector({cl: Factor.uniform(domain.project(cl)) for cl in cliques})

    @staticmethod
    def random(domain, cliques, prng=np.random):
        return CliqueVector(
            {cl: Factor.random(domain.project(cl), prng) for cl in cliques}
        )

    @staticmethod
    def normal(domain, cliques, prng=np.random):
        return CliqueVector(
            {cl: Factor.normal(domain.project(cl), prng) for cl in cliques}
        )

    @staticmethod
    def from_data(data, cliques):
        ans = {}
        for cl in cliques:
            mu = data.project(cl)
            ans[cl] = Factor(mu.domain, mu.datavector())
        return CliqueVector(ans)

    @property
    def domain(self):
        return functools.reduce(Domain.merge, [f.domain for f in self.values()])

    @property
    def cliques(self):
      return list(self.keys())
    
    def normalize(self, total: float=1, log: bool=True):
        return CliqueVector({ cl: self[cl].normalize(total, log) for cl in self})

    def project(self, clique):
      for cl in self.cliques:
        if set(clique) <= set(cl):
          return self[cl].project(clique)
      raise ValueError(f'Cannot project on {clique}.')

    def combine(self, other):
        # combines this CliqueVector with other, even if they do not share the same set of factors
        # used for warm-starting optimization
        # Important note: if other contains factors not defined within this CliqueVector, they
        # are ignored and *not* combined into this CliqueVector
        for cl in other:
            for cl2 in self:
                if set(cl) <= set(cl2):
                    self[cl2] += other[cl]
                    break

    def __mul__(self, const):
        ans = {cl: const * self[cl] for cl in self}
        return CliqueVector(ans)

    def __rmul__(self, const):
        return self.__mul__(const)

    def __add__(self, other):
        if np.isscalar(other):
            ans = {cl: self[cl] + other for cl in self}
        else:
            ans = {cl: self[cl] + other[cl] for cl in self}
        return CliqueVector(ans)

    def __sub__(self, other):
        return self + -1 * other

    def exp(self):
        ans = {cl: self[cl].exp() for cl in self}
        return CliqueVector(ans)

    def log(self):
        ans = {cl: self[cl].log() for cl in self}
        return CliqueVector(ans)

    def dot(self, other):
        return sum((self[cl] * other[cl]).sum() for cl in self)

    def size(self):
        return sum(self[cl].domain.size() for cl in self)
