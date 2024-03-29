"""
This is a test harness that will allow us to test the efficiacy of different
sampling methodologies.
"""
import numpy as np
import logger
import rank_centrality

_log = logger.setup_logger(__name__)

class _Statistics:
    """
    This class maintains various statistics about the state of the simulation,
    and is held by the Harness and is passed to the selector and the
    activator. In particular, it maintains a sample counting array,
    a win matrix, and an active image array.
    """
    def __init__(self, num_images, ground_truth):
        """
        :param num_images: The number of images to run the simulation over.
        :param ground_truth: The ground truth.
        :return: _Statistics object
        """
        self.num_images = num_images
        self.num_samples = np.zeros(num_images, dtype=int)
        self.sampling_gap = -self.num_samples
        self.is_active = np.zeros(num_images, dtype=bool)
        self.num_active = 0
        self.win_matrix = np.zeros((num_images, num_images))
        self.stop_activating = False
        self.pairs = set()
        self._rank = np.zeros(num_images) + 1.
        self._recomp_rank = False
        self._ground_truth = ground_truth

    @property
    def min_seen(self):
        """
        :return: The number of times the least seen active image has been
        sampled.
        """
        try:
            return np.min(self.num_samples[self.is_active])
        except:
            return 0

    @property
    def rank(self):
        if self._recomp_rank:
            self._rank = rank_centrality.rank(self.win_matrix)
            self._recomp_rank = False
        return self._rank[self.is_active]

    def sampling_gap_allow_selection(self, i):
        """
        Modifies the sampling gap and returns whether or not an item can be
        sampled.

        :param i: The item in question.
        :return: True or False, depending on whether the item can be sampled.
        """
        self.sampling_gap[i] += 1
        if self.sampling_gap[i] > 0:
            return True
        return False

    def activate(self, images):
        """
        Images is an array of indices to activate.

        :param images: an array of indices to activate.
        :return: None
        """
        self.is_active[images] = True
        self.num_active = np.sum(self.is_active)
        if self.num_active != self.num_images:
            self.sampling_excess = -self.num_samples
        if self.num_active == self.num_images:
            self.stop_activating = True

    def add_win(self, win, lose):
        """
        Adds an outcome.

        :param win: The index of the winner.
        :param lose: The index of the loser.
        :return: None
        """
        self.pairs.add((win, lose))
        self.pairs.add((lose, win))
        self.win_matrix[win, lose] = 1
        self.num_samples[win] += 1
        self.num_samples[lose] += 1
        self._recomp_rank = True

    def pair_exists(self, i, j):
        """
        Returns True if the pair exists or not.

        :param i: First image idx
        :param j: Second image idx
        :return: Boolean
        """
        return (i, j) in self.pairs


class Harness:
    """
    The test Harness itself.
    """
    def __init__(self,
                 num_images,
                 selector,
                 distribution,
                 activation_criteria,
                 activation_chunk,
                 scoring_metrics):
        """
        Instantiates the test harness.

        :param num_images: The number of images to simulate the test over.
        :param selector: A selector object, which accepts a _Statistics object.
        :param distribution: The distribution from which scores are to be
                             sampled.
        :param activation_criteria: Boolean function that indicates if
                                    another sample should be taken. Accepts a
                                    _Statistics object.
        :param activation_chunk: The activation chunk size.
        :param scoring_metrics: A list of functions that accept the estimated
                                scores and the ground truth and return a score.
        :return: The various scores.
        """
        self.num_images = num_images
        self.selector = selector
        self.distribution = distribution
        self.activation_criteria = activation_criteria
        self.activation_chunk = activation_chunk
        self.scoring_metrics = scoring_metrics
        self.stats = None
        self._ground_truth = None
        self.stop_activating = False
        self.tot_iter = 0
        self.reset()

    @property
    def rank(self):
        return self.stats.rank

    @property
    def ground_truth(self):
        return self._ground_truth[self.stats.is_active]

    def reset(self):
        """
        Resets the experiment.
        """
        self.tot_iter = 0
        self._ground_truth = \
            np.array([self.distribution() for _ in range(self.num_images)])
        self.stats = _Statistics(self.num_images, self._ground_truth)

    def iterate(self, i=1):
        """
        Iterates.

        :param i: The number of iterations to perform
        :returns: None.
        """
        for _ in range(i):
            self._iterate()
        if not self.tot_iter % 100:
            _log.debug('Iteration %i complete' % self.tot_iter)

    def _iterate(self):
        self.tot_iter += 1
        if not self.stats.stop_activating:
            if self.activation_criteria(self.stats):
                to_activate = \
                    np.logical_not(
                        self.stats.is_active
                    ).nonzero()[0][:self.activation_chunk]
                _log.info('Activating more images, %i total' % (
                                    self.stats.num_active))
                self.stats.activate(to_activate)
        i, j = self.selector.get(self.stats)
        sc1 = self._ground_truth[i]
        sc2 = self._ground_truth[j]
        if np.random.rand() < (sc1 / (sc1 + sc2)):
            self.stats.add_win(i, j)
        else:
            self.stats.add_win(j, i)

    def get_scores(self):
        scores = []
        for score_func in self.scoring_metrics:
            scores.append(score_func(self.rank, self.ground_truth))
        return scores


'''
Distributions
    These are functions that accept no arguments but samples from some
    distribution.
'''


def beta_distribution():
    return np.random.beta(2., 5.)


'''
Activators
'''


def orig_activator_generator(gamma):

    def orig_activator(stats):
        if stats.min_seen >= gamma * np.log(stats.num_active):
            return True
        return False
    return orig_activator


def prop_activator_generator(gamma):
    '''works based on proportions instead of minima'''

    def prop_activator(stats):
        if stats.num_active == 0:
            return True
        samp_rat = float(np.sum(stats.num_samples)) / stats.num_active
        if samp_rat >= gamma * np.log(stats.num_active):
            return True
        return False

    return prop_activator

'''
Selectors
'''


class OrigSelector():
    """
    This is approximately how the original selector worked.
    """
    def __init__(self):
        pass

    def prob_select(self, stats):
        base_prob = 2. / stats.num_active
        return lambda time_seen: (base_prob +
                                  (1 - base_prob) * np.exp(
                                      stats.min_seen - time_seen
                                  ))

    def get(self, stats):
        ps = self.prob_select(stats)
        attempts = 0
        while True:
            if attempts > 10000:
                import ipdb
                ipdb.set_trace()
            attempts += 1
            cands = []
            while len(cands) < 2:
                for i in range(stats.num_active):
                    time_seen = stats.num_samples[i]
                    if ps(time_seen) > np.random.rand():
                        cands.append(i)
                        if len(cands) == 2:
                            break
            i, j = cands
            if i != j:
                if not stats.pair_exists(i, j):
                    return i, j


class MinimizeSamplingGapSelector():
    """
    This selector works by only selecting pairs that have a greater-than-0
    sampling gap.
    """
    def __init__(self):
        pass

    def get(self, stats):
        attempts = 0
        while True:
            if attempts > 10000:
                import ipdb
                ipdb.set_trace()
            attempts += 1
            cands = []
            while len(cands) < 2:
                for i in range(stats.num_active):
                    if 2. / stats.num_active > np.random.rand():
                        if stats.sampling_gap_allow_selection(i):
                            cands.append(i)
                            if len(cands) == 2:
                                break
            i, j = cands
            if i != j:
                if not stats.pair_exists(i, j):
                    return i, j


class RandomSelector():
    """
    Operates purely randomly.
    """
    def __init__(self):
        pass

    def get(self, stats):
        attempts = 0
        while True:
            if attempts > 10000:
                import ipdb
                ipdb.set_trace()
            attempts += 1
            i = np.random.choice(stats.num_images)
            j = np.random.choice(stats.num_images)
            if i != j:
                break
        return i, j


class OracleSelector():
    """
    Selects pairs that are close together in score.
    """
    def __init__(self):
        self._arg_srt_gt = None
        pass

    def get(self, stats):
        self._arg_srt_gt = list(np.argsort(stats._ground_truth[
                                           :stats.num_active]))
        attempts = 0
        while True:
            if attempts > 10000:
                import ipdb
                ipdb.set_trace()
            attempts += 1
            i = np.random.choice(stats.num_active)
            i_idx = self._arg_srt_gt.index(i)
            j_idx = i_idx + int(np.random.randn() * 5)
            if j_idx < 0:
                continue
            if j_idx >= stats.num_active:
                continue
            if i_idx == j_idx:
                continue
            j = self._arg_srt_gt[j_idx]
            return i, j


class OnlineOracleSelector():
    """
    Attempts to make oracular selections, but in an online fashion. While it
    does not have access to the ground truth, it does have access to the
    computed values.
    """
    def __init__(self):
        self._arg_srt_gt = []
        pass

    def get(self, stats):
        if len(self._arg_srt_gt) != stats.num_active:
            self._arg_srt_gt = list(np.argsort(stats.rank))
        # recompute rank probabilistically
        # elif np.random.rand() < 0.01:
        #     self._arg_srt_gt = np.argsort(stats.rank)
        attempts = 0
        while True:
            if attempts > 10000:
                import ipdb
                ipdb.set_trace()
            attempts += 1
            i = np.random.choice(stats.num_active)
            if not stats.sampling_gap_allow_selection(i):
                continue
            i_idx = self._arg_srt_gt.index(i)
            for d_j_idx in range(1, stats.num_active):
                j_idx = i_idx + d_j_idx
                if j_idx >= stats.num_active:
                    continue
                if not stats.pair_exists(i, self._arg_srt_gt[j_idx]):
                    return i, self._arg_srt_gt[j_idx]
                j_idx = i_idx - d_j_idx
                if j_idx < 0:
                    continue
                if not stats.pair_exists(i, self._arg_srt_gt[j_idx]):
                    return i, self._arg_srt_gt[j_idx]


'''
Scoring Metrics

For these, higher is *always* better!
'''


def weighted_kemeny_distance(o, w):
    """
    The scoring metric in the Shah paper.

    :param o: The item-wise calculated scores.
    :param w: The item-wise ground truth scores.
    :return: The score quantity.
    """
    sum_score = 0
    for j in range(len(w)):
        for i in range(j):
            # note that the formula in the shaw paper is wrong.
            q = (w[i] - w[j])**2
            if ((w[i] - w[j])*(o[i] - o[j])) <= 0:
                sum_score += q
    denom = 2 * len(w) * np.sum(w ** 2)
    return 1 - np.sqrt(sum_score / denom)


def weighted_kemeny_distance_ratio(o, w):
    """
    The scoring metric in the Shah paper.

    :param o: The item-wise calculated scores.
    :param w: The item-wise ground truth scores.
    :return: The score quantity.
    """
    sum_score = 0
    tot_pos_score = 0  # the upper limit of the bad scores
    for j in range(len(w)):
        for i in range(j):
            # note that the formula in the shaw paper is wrong.
            q = (w[i] - w[j])**2
            tot_pos_score += q
            if ((w[i] - w[j])*(o[i] - o[j])) <= 0:
                sum_score += q
    denom = 2 * len(w) * np.sum(w ** 2)
    return 1 - np.sqrt(sum_score / denom) / np.sqrt(tot_pos_score / denom)

def plot_win_mtx_by_sco(h):
    """
    :param h: a harness object
    :return: None
    """
    asrt = np.argsort(h.ground_truth)
    xmtx = h.stats.win_matrix[asrt,:][:,asrt]
    pcolor(xmtx + xmtx.T)

def corrcoef_score(o, w):
    return np.corrcoef(o, w)[0,1]

# our empirical estimates suggest gamma = 8.6, and we need 8.6 * n * log(n)
# samples, when using the
close('all')
fig = figure()

# kemeny_online_oracle = []
# for i in range(1, 100001):
#     if not i % 100:
#         sco = h_online_oracle.get_scores()
#         kemeny_online_oracle.append(sco[0])
#         cla()
#         plot(kemeny_online_oracle, label='kemeny_online_oracle')
#         legend(loc='best')
#         pause(0.1)
#     h_online_oracle.iterate()


results = []
for j in range(100):
    gamma = 7.5
    orig_activator = orig_activator_generator(gamma)
    prop_activator = prop_activator_generator(gamma)
    os = OrigSelector()
    msgs = MinimizeSamplingGapSelector()
    rs = RandomSelector()
    ors = OracleSelector()
    oors = OnlineOracleSelector()

    m = 500
    h_o = Harness(m, os, beta_distribution, prop_activator, 100,
                 [weighted_kemeny_distance_ratio])
    h_msgs = Harness(m, msgs, beta_distribution, prop_activator, 100,
                     [weighted_kemeny_distance_ratio])
    h_rand = Harness(m, rs, beta_distribution, prop_activator, 100,
                     [weighted_kemeny_distance_ratio])
    h_oracle = Harness(m, ors, beta_distribution, prop_activator, 100,
                       [weighted_kemeny_distance_ratio])
    h_online_oracle = Harness(m, oors, beta_distribution, prop_activator, 100,
                              [weighted_kemeny_distance_ratio])
    kemeny_o = []
    kemeny_msgs = []
    kemeny_rand = []
    kemeny_oracle = []
    kemeny_online_oracle = []
    for i in range(1, m*25+1):
        if not i % 100:
            sco = h_o.get_scores()
            kemeny_o.append(sco[0])
            sco = h_msgs.get_scores()
            kemeny_msgs.append(sco[0])
            sco = h_rand.get_scores()
            kemeny_rand.append(sco[0])
            sco = h_online_oracle.get_scores()
            kemeny_online_oracle.append(sco[0])
            # cla()
            # plot(kemeny_o, label='kemeny_orig')
            # plot(kemeny_msgs, label='kemeny_msgs')
            # plot(kemeny_rand, label='kemeny_rand')
            # plot(kemeny_online_oracle, label='kemeny_online_oracle')
            # legend(loc='best')
            # pause(0.1)
        h_o.iterate()
        h_msgs.iterate()
        h_rand.iterate()
        h_online_oracle.iterate()
    results.append([kemeny_o, kemeny_msgs, kemeny_rand, kemeny_online_oracle])

# acquire the means of the various results
m_orig = np.mean(np.array([x[0] for x in results]), 0)
m_msgs = np.mean(np.array([x[1] for x in results]), 0)
m_rand = np.mean(np.array([x[2] for x in results]), 0)
m_oracle = np.mean(np.array([x[3] for x in results]), 0)
# cla()
plot(m_orig, label='kemeny_orig')
plot(m_msgs, label='kemeny_msgs')
plot(m_rand, label='kemeny_rand')
plot(m_oracle, label='kemeny_oracle')
legend(loc='best')
