"""
Exports useful utilities for generation.
"""

import numpy as np
from itertools import combinations as comb

def get_design(n, t, j):
    """
    Creates an experimental design by creating a series of fixed-length subsets of N elements such that each element
    appears at least some number of times and no pairs of elements occurs in any subset more than once. The number of
    subsets is minimized. Each subset can be appropriately conceptualized as a "trial."

    This constitutes an incomplete t-Design. It effectively extends t-Designs to a new type of design,
    t_min-(v, k, lambda, x), which is an incidence structure such that:
        - (1) There are v points.
        - (2) Each block contains k points.
        - (3) For any t points there are exactly lambda blocks that contain all these points.
        - (4) Each point occurs in at least x blocks.
        - (5) No block can be removed without violating 1-4, i.e., it is 'minimal' in a sense.

    See:
        --- The general format is a T-design: http://mathworld.wolfram.com/t-Design.html
        --- In our case, because pairs must occur precisely once, it is a Steiner System:
            http://mathworld.wolfram.com/SteinerSystem.html

    If no such design is possible, returns None.

    This generates a Steiner system deterministically--due to the hypergeometric rate of expansion, it is not possible
    to generate unique Steiner systems each time without using a random component, which makes things so much messier.
    I may introduce such a method later on. It is not clear if this determinism will introduce a bias in the responses.
    My guess is that, under random assignment of images to indices, no bias is possible--but I'm not sure if thats true.

    :param n: The number of distinct elements involved in the experiment.
    :param t: The number of elements to present each trial.
    :param j: The number of times each element should appear during the experiment.
    :return: A list of tuples representing each subset. Elements may be randomized within element and subset order may
    be randomized without consequence.
    """
    obs = np.zeros((n, n)) # pair observation matrix
    occ = np.zeros(n) # counter for the number of observations
    combs = [] # the combinations that will be returned.
    for allvio in range(t): # minimize the number of j-violations (i.e., elements appearing more than j-times)
        for c in comb(range(n), t):
            if np.min(occ) == j:
                return combs # you're done
            cvio = 0 # the current count of violations
            for x1, x2 in comb(c, 2):
                if obs[x1, x2]:
                    continue
            for i in c:
                cvio += max(0, occ[i] - j + 1)
            if cvio > allvio:
                continue
            for x1, x2 in comb(c, 2):
                obs[x1, x2] += 1
            for i in c:
                occ[i] += 1
            combs.append(c)
    if not np.min(occ) >= j:
        return None
    return combs
