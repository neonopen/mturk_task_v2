"""
Exports the Set and Get classes for interacting with the database. The
segregation between set and get methods is strict in terms of putting data
into the database: nothing in Get() modifies the database. Elements of Set()
may read from the database internally, but this is never surfaced to the
invoking frame.

Note:
    The above is no longer strictly true; Get() methods for obtaining images
    do modify the database, but only counters that are not surfaced to the user.

For workers, interval counts are the most recent 'buckets' of results. These
data are used for bans, etc. They are reset once the number of tasks that are
in the bucket exceeds a value set in conf.py, or once a worker is unbanned.
"""

from conf import *
from dill import dumps
from dill import loads
from itertools import combinations as comb
from datetime import datetime
import time
from collections import Counter
import scipy.stats as stats
import json
from geoip import geolite2

"""
LOGGING
"""

_log = logger.setup_logger(__name__)

"""
PRIVATE METHODS
"""


def _get_pair_key(image1, image2):
    """
    Returns a row key for a given pair. Row keys for pairs are the image IDs,
    separated by a comma, and sorted alphabetically. The inputs need not be
    sorted alphabetically.

    :param image1: Image 1 ID.
    :param image2: Image 2 ID.
    :return: The pair row key, as a string.
    """
    return ','.join(pair_to_tuple(image1, image2))


def _get_preexisting_pairs(conn, images):
    """
    Returns all pairs that have already occurred among a list of images. The
    original implementation is potentially more robust (
    _get_preexisting_pairs_slow), but has polynomial complexity, which is
    obviously undesirable. The conceptual similarity (as mentioned in the
    original implementation) is not worth the added time complexity,
    especially when we may be generating thousands of tasks.

    :param conn: The HappyBase connection object.
    :param images: A iterable of image IDs
    :return: A set of tuples of forbidden image ID pairs.
    """
    found_pairs = set()
    table = conn.table(IMAGE_TABLE)
    im_set = set(images)
    for im1 in images:
        scanner = table.scan(row_prefix=im1,
                             columns=['metadata:im_id1', 'metadata:im_id2'])
        for row_key, row_data in scanner:
            # don't use get with this, we want it to fail hard if it does.
            c_im1 = row_data['metadata:im_id1']
            c_im2 = row_data['metadata:im_id2']
            if c_im1 in im_set and c_im2 in im_set:
                found_pairs.add(pair_to_tuple(c_im1, c_im2))
    return found_pairs


def _prob_select(base_prob, min_seen):
    """
    Returns a lambda function giving the probability of an image being
    selected for a new task.

    NOTES:
        The function is given by:
            BaseProb + (1 - BaseProb) * 2 ^ -(times_seen - min_seen)

        As a private function, it assumes the input is correct and does not
        check it.

    :param base_prob: Base probability of selection
    :param min_seen: The global min for the number of times an image has been
                     in a task.
    :return: A lambda function that accepts a single parameter (times_seen) and
             returns the probability of selecting
             this image.
    """
    return lambda time_seen: base_prob + (1. - base_prob) * \
                                         (2 ** (time_seen - min_seen))


def _pair_exists(conn, pair_key):
    """
    Returns True if a pair already exists in the database.

    :param conn: The HappyBase connection object.
    :param pair_key: The key to search for the pair with.
    :return: True if pair exists, False otherwise.
    """
    table = conn.table(PAIR_TABLE)
    return Get.table_has_row(table, pair_key)


def _tuple_permitted(im_tuple, ex_pairs, conn=None):
    """
    Returns True if the tuple is allowable.

    Note:
        If both table and conn are omitted, then the search is only performed
        over the extant (i.e., in-memory) pairs.

    :param im_tuple: The candidate tuple.
    :param ex_pairs: The pairs already made in this task. (in the form of
                     pair_to_tuple)
    :param conn: The HappyBase connection object.
    :return: True if this tuple may be added, False otherwise.
    """
    # First, check to make sure none of them are already in *this* task,
    # which is much cheaper than the database.
    for im1, im2 in comb(im_tuple, 2):
        pair = pair_to_tuple(im1, im2)
        if pair in ex_pairs:
            return False
    if conn is None:
        return True  # The database cannot be checked, and so we will be only
    for im1, im2 in comb(im_tuple, 2):
        pair_key = _get_pair_key(im1, im2)
        if _pair_exists(conn, pair_key):
            return False
    return True


def _timestamp_to_struct_time(timestamp):
    """
    Converts an HBase timestamp (msec since UNIX epoch) to a struct_time.

    :param timestamp: The HappyBase (i.e., HBase) timestamp, as an int.
    :return: A struct_time object.
    """
    return time.localtime(float(timestamp)/1000)


def _get_ban_expiration_date_str(ban_issued, ban_length):
    """
    Returns the date a ban expires.

    :param ban_issued: The timestamp the ban was issued on, an int, in HBase
                       / HappyBase format (msec since epoch)
    :param ban_length: The duration of the ban, an int, seconds.
    :return: The date the ban expires, as a string.
    """
    expire_time = ban_issued + ban_length
    return _get_timedelta_string(expire_time, time.mktime(time.localtime()))


def _get_timedelta_string(timestamp1, timestamp2):
    """
    Returns a string, time from now, when the ban expires.

    :param timestamp1: First timestamp, as an int in HBase / Happybase style
                       (msec since epoch)
    :param timestamp2: Second timestamp, as an int in HBase / Happybase style
                       (msec since epoch)
    :return: The timedelta, in weeks, days, hours, minutes and seconds,
             as a string.
    """
    week_len = 7 * 24 * 60 * 60.
    day_len = 24 * 60 * 60.
    hour_len = 60 * 60.
    min_len = 60.
    time_delta = abs(float(timestamp1)/1000 - float(timestamp2)/1000)
    weeks, secs = divmod(time_delta, week_len)
    days, secs = divmod(secs, day_len)
    hours, secs = divmod(secs, hour_len)
    minutes, secs = divmod(secs, min_len)
    time_list = [[weeks, 'week'], [days, 'day'], [hours, 'hour'],
                 [minutes, 'minute'], [secs, 'second']]

    def get_name(num, noun):
        if not num:
            return ''
        if num == 1:
            return '%i %s' % (num, noun)
        else:
            return '%i %ss' % (num, noun)
    cur_strs = []
    for num, noun in time_list:
        cur_strs.append(get_name(num, noun))
    return ', '.join(filter(lambda x: len(x), cur_strs))


def _create_table(conn, table, families, clobber):
    """
    General create table function.

    :param conn: The HappyBase connection object.
    :param table: The table name.
    :param families: The table families, see conf.py
    :param clobber: Boolean, if true will erase old workers table if it exists.
                    [def: False]
    :return: True if table was created. False otherwise.
    """
    if table in conn.tables():
        if not clobber:
            # it exists and you can't do anything about it.
            return False
        # delete the table
        conn.delete_table(table, disable=True)
    conn.create_table(table, families)
    return table in conn.tables()


def _conv_dict_vals(data):
    """
    Converts a dictionary's values to strings, so they can be stored in HBase.

    :param data: The dictionary you wish to store.
    :return: The converted dictionary.
    """
    for k, v in data.iteritems():
        if v is None:
            data[k] = ''
        elif type(v) is bool:
            if v:
                data[k] = TRUE
            else:
                data[k] = FALSE
        else:
            try:
                data[k] = FLOAT_STR % v
            except TypeError:
                data[k] = str(v)
    return data


def _create_arbitrary_dict(data, prefix):
    """
    Converts a list of items into a numbered dict, in which the keys are the
    item indices.

    :param data: A list of items.
    :return: A dictionary that can be stored in HBase
    """
    return {prefix + ':%i' % n: v for n, v in enumerate(data)}


def _get_image_dict(image_url):
    """
    Returns a dictionary for image data, appropriate for inputting into the
    image table.

    :param image_url: The URL of the image, as a string.
    :return: If the image can be found and opened, a dictionary. Otherwise None.
    """
    width, height = get_im_dims(image_url)
    if width is None:
        return None
    aspect_ratio = '%.3f'%(float(width) / height)
    im_dict = {'metadata:width': width, 'metadata:height': height,
               'metadata:aspect_ratio': aspect_ratio,
               'metadata:url': image_url, 'metadata:is_active': FALSE}
    return _conv_dict_vals(im_dict)


def _get_pair_dict(image1, image2, task_id, attribute):
    """
    Creates a dictionary appropriate for the creation of a pair entry in the
    Pairs table.

    NOTE:
        In the table, imId1 is always the lexicographically first image.

    :param image1: Image 1 ID.
    :param image2: Image 2 ID.
    :param task_id: The task ID.
    :param attribute: The task attribute.
    :return: A dictionary for use as hbase input.
    """
    if image1 > image2:
        im1 = image2
        im2 = image1
    else:
        im1 = image1
        im2 = image2
    pair_dict = {'metadata:im_id1': im1, 'metadata:im_id2': im2,
                 'metadata:task_id': task_id,
                 'metadata:attribute': attribute}
    return _conv_dict_vals(pair_dict)


def _find_demographics_element_in_json(resp_json):
    """
    Given the response json, it returns the block that contains the
    demographics data.

    :param resp_json: The response JSON from mturk.
    :return: The JSON block that contains the demographics element. If it
             cannot find any, then it returns None.
    """
    if type(resp_json) is dict:
        if 'birthyear' in resp_json:
            return resp_json
        elif 'gender' in resp_json:
            return resp_json
        else:
            return None
    for i in resp_json:
        if 'birthyear' in i:
            return i
        elif 'gender' in i:
            return i
        else:
            return None


def _shuffle_tuples(image_tuples):
    """
    Shuffles a list of image tuples, both the list itself and within-tuple.

    NOTE:
        This shuffling is not done in place.

    :param image_tuples: A list of image tuples.
    :return: The shuffled list of image tuples and a global mapping of tuples to
             invariant indices based on their initial position in the input
             array (so they can be easily mapped back).
    """
    tup_indices = np.arange(len(image_tuples))
    np.random.shuffle(tup_indices)  # shuffle the tuple indices in-place.
    n_tuples = list(np.array(image_tuples)[tup_indices])
    for n, tup in enumerate(n_tuples):
        ltup = list(tup)
        np.random.shuffle(ltup)
        n_tuples[n] = ltup
    # make make absolutely sure that tup_indices is cast to a list,
    # since numpy arrays do *not* work well with jinja2
    return n_tuples, list(tup_indices)


"""
Main Classes - GET
"""


class Get(object):
    """
    Handles general query events for the task. These are more abstract than
    the 'set' functions, as there is a larger variety of possibilities here.
    """
    def __init__(self, conn):
        """
        :param conn: The HappyBase / HBase connection object.
        :return: A Get instance.
        """
        self.conn = conn
        self._last_active_im = None  # stores the last active image scanned
    
    def worker_exists(self, worker_id):
        """
        Indicates whether we have a record of this worker in the database.

        :param worker_id: The Worker ID (from MTurk), as a string.
        :return: True if the there are records of this worker, otherwise false.
        """
        table = self.conn.table(WORKER_TABLE)
        return self.table_has_row(table, worker_id)

    def worker_need_demographics(self, worker_id):
        """
        Indicates whether or not the worker needs demographic information.

        :param worker_id: The Worker ID (from MTurk), as a string.
        :return: True if we need demographic information from the worker. False
                 otherwise.
        """
        table = self.conn.table(WORKER_TABLE)
        row_data = table.row(worker_id)
        if not len(row_data.get('demographics:gender', '')):
            return True
        else:
            return False

    def get_all_workers(self):
        """
        Iterates over all defined workers.

        :return: An iterator over workers, which returns worker IDs.
        """
        table = self.conn.table(WORKER_TABLE)
        scanner = table.scan(filter=b'KeyOnlyFilter() AND FirstKeyOnlyFilter()')
        for row_key, data in scanner:
            yield row_key
        return

    def worker_need_practice(self, worker_id):
        """
        Indicates whether the worker should be served a practice or a real task.

        :param worker_id: The Worker ID (from MTurk), as a string.
        :return: True if the worker has not passed a practice yet and must be
                 served one. False otherwise.
        """
        table = self.conn.table(WORKER_TABLE)
        row_data = table.row(worker_id)
        return row_data.get('status:passed_practice', FALSE) != TRUE

    def current_worker_practices_number(self, worker_id):
        """
        Returns which practice the worker needs (as 0 ... N)

        :param worker_id: The Worker ID (from MTurk) as a string.
        :return: An integer corresponding to the practice the worker needs
                 (starting from the top 'row')
        """
        table = self.conn.table(WORKER_TABLE)
        row_data = table.row(worker_id)
        return int(row_data.get(
            'status:num_practices_attempted_interval', '0'))

    def worker_is_banned(self, worker_id):
        """
        Determines whether or not the worker is banned.

        :param worker_id: The Worker ID (from MTurk), as a string.
        :return: True if the worker is banned, False otherwise.
        """
        table = self.conn.table(WORKER_TABLE)
        data = table.row(worker_id, columns=['status:is_banned'])
        return data.get('status:is_banned', FALSE) == TRUE

    def get_worker_ban_time_reason(self, worker_id):
        """
        Returns the length of remaining time this worker is banned along with
        the reason as a tuple.

        :param worker_id: The Worker ID (from MTurk), as a string.
        :return: The time until the ban expires and the reason for the ban.
        """
        if not self.worker_is_banned(worker_id):
            return None, None
        table = self.conn.table(WORKER_TABLE)
        data = table.row(worker_id,
                         columns=['status:ban_length', 'status:ban_reason'],
                         include_timestamp=True)
        ban_time, timestamp = data.get('status:ban_length',
                                       (DEFAULT_BAN_LENGTH, 0))
        ban_reason, _ = data.get('status:ban_reason', (DEFAULT_BAN_REASON, 0))
        return _get_timedelta_string(int(ban_time * 1000), timestamp), \
               ban_reason

    def worker_attempted_interval(self, worker_id):
        """
        Returns the number of tasks this worker has attempted this week.

        :param worker_id: The worker ID, as a string.
        :return: Integer, the number of tasks the worker has attempted this
                 week.
        """
        table = self.conn.table(WORKER_TABLE)
        data = table.row(worker_id, columns=['stats:num_attempted_interval'])
        return int(data.get('stats:num_attempted_interval', '0'))

    def worker_attempted_too_much(self, worker_id):
        """
        Returns True if the worker has attempted too many tasks.

        :param worker_id: The worker ID, as a string.
        :return: True if the worker has attempted too many tasks, otherwise
                 False.
        """
        _log.warning('DEPRECATED: This functionality is now performed '
                     'implicitly by the MTurk task structure')
        return self.worker_attempted_interval(worker_id) > \
               (7 * MAX_SUBMITS_PER_DAY)

    def worker_weekly_rejected(self, worker_id):
        """
        Returns the rejection-to-acceptance ratio for this worker for this week.

        :param worker_id: The worker ID, as a string.
        :return: Float, the number of tasks rejected divided by the number of
                 tasks accepted.
        """
        table = self.conn.table(WORKER_TABLE)
        return table.counter_get(worker_id, 'stats:num_rejected_interval')

    def worker_weekly_reject_accept_ratio(self, worker_id):
        """
        Returns the rejection-to-acceptance ratio for this worker for this week.

        :param worker_id: The worker ID, as a string.
        :return: Float, the number of tasks rejected divided by the number of
                 tasks accepted.
        """
        table = self.conn.table(WORKER_TABLE)
        num_acc = float(table.counter_get(worker_id,
                                          'stats:num_accepted_interval'))
        num_rej = float(table.counter_get(worker_id,
                                          'stats:num_rejected_interval'))
        return num_rej / num_acc

    # TASK
    
    def get_task_status(self, task_id):
        """
        Fetches the status code given a task ID.

        :param task_id: The task ID, which is the row key.
        :return: A status code, as defined in conf.
        """
        table = self.conn.table(TASK_TABLE)
        if not self.table_has_row(table, task_id):
            return DOES_NOT_EXIST
        task = table.row(task_id)
        if task.get('metadata:is_practice', FALSE) == TRUE:
            return IS_PRACTICE
        if task.get('status:awaiting_serve', FALSE) == TRUE:
            if task.get('status:awaiting_hit_group', FALSE) == TRUE:
                return AWAITING_HIT
            return AWAITING_SERVE
        if task.get('status:pending_completion', FALSE) == TRUE:
            return COMPLETION_PENDING
        if task.get('status:pending_evaluation', FALSE) == TRUE:
            return EVALUATION_PENDING
        if task.get('status:accepted', FALSE) == TRUE:
            return ACCEPTED
        if task.get('status:rejected', FALSE) == TRUE:
            return REJECTED
        return UNKNOWN_STATUS

    def get_n_with_hit_awaiting_serve(self):
        """
        Returns the number of true tasks that are awaiting serving.

        :return: None
        """
        row_filter = general_filter([('status', 'awaiting_hit_type'),
                                     ('status', 'awaiting_serve')],
                                    [FALSE, TRUE], key_only=False)
        scanner = self.conn.table(TASK_TABLE).scan(filter=row_filter)
        awaiting_serve_cnt = 0
        for _ in scanner:
            awaiting_serve_cnt += 1
        return awaiting_serve_cnt

    def get_task_blocks(self, task_id):
        """
        Returns the task blocks, as a list of dictionaries, appropriate for
        make_html.

        :param task_id: The task ID, as a string.
        :return: List of blocks represented as dictionaries, if there is a
                 problem returns None.
        """
        table = self.conn.table(TASK_TABLE)
        pickled_blocks = \
            table.row(task_id, columns=['blocks:c1']).get('blocks:c1', None)
        if pickled_blocks is None:
            return None
        blocks = loads(pickled_blocks)
        url_map = dict()
        table = self.conn.table(IMAGE_TABLE)
        # convert the image IDs into URLs
        for block in blocks:
            for im_list in block['images']:
                for image in im_list:
                    if image not in url_map:
                        url_map[image] = \
                            table.row(image).get('metadata:url', None)
        # now, replace this image IDs with their URLs
        for block in blocks:
            for n, im_list in enumerate(block['images']):
                block['images'][n] = [url_map[x] for x in im_list]
        return blocks

    def task_is_practice(self, task_id):
        """
        Indicates whether or not the task in question is a practice.

        :param task_id: The task ID, as a string.
        :return: Boolean. Returns True if the task specified by the task ID
                 is a practice, otherwise false.
        """
        table = self.conn.table(TASK_TABLE)
        return table.row(task_id).get('metadata:is_practice', FALSE) == TRUE

    # HIT TYPES
    
    def get_hit_type_info(self, hit_type_id):
        """
        Returns the information for a hit_type_id.

        :param hit_type_id: The HIT type ID, as provided by mturk.
        :return: The HIT Type information, as a dictionary. If this hit_type_id
                 does not exist, returns an empty dictionary.
        """
        table = self.conn.table(HIT_TYPE_TABLE)
        return table.row(hit_type_id)

    def hit_type_matches(self, hit_type_id,
                         task_attribute=ATTRIBUTE,
                         image_attributes=IMAGE_ATTRIBUTES):
        """
        Indicates whether or not the hit is an appropriate match for.
    
        NOTE:
            This is a bit wonky, as the image_attributes for task types (which

        :param hit_type_id: The HIT type ID, as provided by mturk (see
                            webserver.mturk.register_hit_type_mturk).
        :param task_attribute: The task attribute for tasks that are HITs
                               assigned to this HIT type.
        :param image_attributes: The image attributes for tasks that are HITs
                                 assigned to this HIT type.
        :return: True if hit_type_id corresponds to a HIT type that has the
                 specified task attribute and the specified image attributes.
        """
        type_info = self.get_hit_type_info(hit_type_id)
        if type_info == {}:
            _log.warning('No such hit type')
            return False
        if task_attribute != type_info.get('metadata:task_attribute', None):
            return False
        try:
            db_hit_type_image_attributes = \
                loads(type_info.get('metadata:image_attributes',
                                    dumps(IMAGE_ATTRIBUTES)))
        except:
            db_hit_type_image_attributes = set()
        if set(image_attributes) != db_hit_type_image_attributes:
            return False
        return True

    def get_active_hit_type_for(self, task_attribute=ATTRIBUTE,
                                image_attributes=IMAGE_ATTRIBUTES):
        """
        Returns an active hit type ID for some given constraints (
        task_attribute and image_attributes).

        :param task_attribute: The task attribute for tasks that are HITs
                               assigned to this HIT type.
        :param image_attributes: The image attributes for tasks that are HITs
                                 assigned to this HIT type.
        :return: The active HIT Type ID, otherwise returns None.
        """
        for hid_type_id, _ in self.get_active_hit_types():
            if self.hit_type_matches(hid_type_id,
                                     task_attribute,
                                     image_attributes):
                return hid_type_id

    def get_active_practice_hit_type_for(self, task_attribute=ATTRIBUTE,
                                         image_attributes=IMAGE_ATTRIBUTES):
        """
        Returns an active practice hit type ID for some given constraints (
        task_attribute and image_attributes).

        :param task_attribute: The task attribute for tasks that are HITs
                               assigned to this HIT type.
        :param image_attributes: The image attributes for tasks that are HITs
                                 assigned to this HIT type.
        :return: The active HIT Type ID, otherwise returns None.
        """
        for hid_type_id, _ in self.get_active_practice_hit_types():
            if self.hit_type_matches(hid_type_id,
                                     task_attribute,
                                     image_attributes):
                return hid_type_id

    def get_active_hit_types(self):
        """
        Obtains active hit types which correspond to non-practice tasks.

        :return: An iterator over active hit types.
        """
        row_filter = \
            general_filter([('status', 'active'), ('metadata', 'is_practice')],
                           values=[TRUE, FALSE],
                           key_only=False)
        return self.conn.table(HIT_TYPE_TABLE).scan(filter=row_filter)

    def get_active_practice_hit_types(self):
        """
        Obtains active hit types that correspond to practice tasks.

        :return: An iterator over active practice hit types.
        """
        row_filter = \
            general_filter([('status', 'active'), ('metadata', 'is_practice')],
                           values=[TRUE, TRUE], key_only=False)
        return self.conn.table(HIT_TYPE_TABLE).scan(filter=row_filter)

    # GENERAL QUERIES

    def table_exists(self, table_name):
        """
        Checks if a table exists.

        :param table_name: The name of the table to check for existence.
        :return: True if table exists, false otherwise.
        """
        return table_name in self.conn.tables()

    @staticmethod
    def table_has_row(table, row_key):
        """
        Determines if a table has a defined row key or not.
    
        :param table: A HappyBase table object.
        :param row_key: The desired row key, as a string.
        :return: True if key exists, false otherwise.
        """
        scan = table.scan(row_start=row_key,
                          filter='KeyOnlyFilter() AND FirstKeyOnlyFilter()',
                          limit=1)
        return next(scan, None) is not None

    @staticmethod
    def get_num_items(table):
        """
        Counts the number of rows in a table.
    
        NOTES: This is likely to be pretty inefficient.
    
        :param table: A HappyBase table object.
        :return: An integer, the number of rows in the object.
        """
        x = table.scan(filter=b'KeyOnlyFilter() AND FirstKeyOnlyFilter()')
        tot_ims = 0
        for _ in x:
            tot_ims += 1
        return tot_ims

    @staticmethod
    def get_items(table):
        """
        Gets all the items represented in a table.
    
        :param table: A HappyBase table object.
        :return: The items in the table.
        """
        scanner = table.scan(filter=b'KeyOnlyFilter() AND FirstKeyOnlyFilter()')
        keys = []
        for key, d in scanner:
            keys.append(key)
        return keys

    # IMAGE STUFF

    def get_active_image_scanner(self, image_attributes=IMAGE_ATTRIBUTES):
        """
        Returns a generator over active images. I'm not sure about the
        behavior of persistent scan() objects, so this function will modify
        the _last_active_im attribute of the class so it can 'remember' where
        it left off each time. This will iterate indefinitely over images,
        looping over the list again and again, until it is destructed. When a
        new one is instaniated, it will pick up where the previous one left off.

        :param image_attributes: The image attributes that the images
                                 considered must satisfy.
        :return: A generator over active images which match the attribute
                 criteria.
        """
        table = self.conn.table(IMAGE_TABLE)
        scanner = table.scan(row_start=self._last_active_im,
                             columns=['metadata:is_active'],
                             filter=attribute_image_filter(image_attributes,
                                                           only_active=True))
        if self._last_active_im is not None:
            # the row_start argument is inclusive, so if _last_active_im is not
            # none, then it will first return the last seen image--so we need
            # to omit it.
            try:
                _ = scanner.next()
            except StopIteration:
                self._last_active_im = None
                scanner = table.scan(row_start=self._last_active_im,
                             columns=['metadata:is_active'],
                             filter=attribute_image_filter(image_attributes,
                                                           only_active=True))
        while True:
            try:
                im_id, _ = scanner.next()
            except StopIteration:
                self._last_active_im = None
                scanner = table.scan(row_start=self._last_active_im,
                             columns=['metadata:is_active'],
                             filter=attribute_image_filter(image_attributes,
                                                           only_active=True))
                im_id, _ = scanner.next()
            self._last_active_im = im_id
            yield im_id

    def get_n_active_images_count(self, image_attributes=IMAGE_ATTRIBUTES):
        """
        Gets a count of active images.

        :param image_attributes: The image attributes that the images
                                 considered must satisfy.
        :return: An integer, the number of active images.
        """
        table = self.conn.table(IMAGE_TABLE)
        # note: do NOTE use binary prefix, because 1 does not correspond to the
        # string 1, but to the binary 1.
        scanner = table.scan(columns=['metadata:is_active'],
                             filter=attribute_image_filter(image_attributes,
                                                           only_active=True))
        active_image_count = 0
        for _ in scanner:
            active_image_count += 1
        return active_image_count

    def get_active_images(self, image_attributes=IMAGE_ATTRIBUTES):
        """
        Gets the IDs of all active images.

        :param image_attributes: The image attributes that the images
                                 considered must satisfy.
        :return: A list of active images
        """
        table = self.conn.table(IMAGE_TABLE)
        # note: do NOTE use binary prefix, because 1 does not correspond to the
        # string 1, but to the binary 1.
        scanner = table.scan(columns=['metadata:is_active'],
                             filter=attribute_image_filter(image_attributes,
                                                           only_active=True))
        active_images = []
        for im_key, im_data in scanner:
            active_images.append(im_key)
        return active_images

    def image_is_active(self, image_id):
        """
        Returns True if an image has been registered into the database and is
        an active image.

        :param image_id: The image ID, which is the row key.
        :return: True if the image is active. False otherwise.
        """
        table = self.conn.table(IMAGE_TABLE)
        is_active = table.row(image_id,
                              columns=['metadata:is_active']
                              ).get('metadata:is_active', None)
        if is_active == TRUE:
            return True
        else:
            return False

    def image_get_min_seen(self, image_attributes=IMAGE_ATTRIBUTES):
        """
        Returns the number of times the least-seen image has been seen.
    
        NOTES: This only applies to active images.

        :param image_attributes: The image attributes that the images
                                 considered must satisfy.
        :return: Integer, the min number of times seen.
        """
        obs_min = np.inf
        table = self.conn.table(IMAGE_TABLE)
        # note that if we provide a column argument, rows without this column
        #  are not emitted.
        scanner = table.scan(columns=['stats:num_times_seen'],
                             filter=attribute_image_filter(image_attributes,
                                                           only_active=True))
        been_seen = 0
        for row_key, _ in scanner:
            been_seen += 1
            cur_seen = table.counter_get(row_key, 'stats:num_times_seen')
            if cur_seen < obs_min:
                obs_min = cur_seen
            if obs_min == 0:
                return 0  # it can't go below 0, so you can cut the scan short
        if not been_seen:
            return 0  # this is an edge case, where none of the images have
            # been seen.
        return obs_min

    def image_get_mean_seen(self, image_attributes=IMAGE_ATTRIBUTES):
        """
        Returns the average number of times an active image has been seen.

        :param image_attributes: The image attributes that the images
                                 considered must satisfy.
        :return: Integer, the min number of times seen.
        """
        table = self.conn.table(IMAGE_TABLE)
        scanner = table.scan(columns=['stats:num_times_seen'],
                             filter=attribute_image_filter(image_attributes,
                                                           only_active=True))
        tot_ims = 0
        tot_seen = 0
        for row_key, _ in scanner:
            tot_ims += 1
            tot_seen += table.counter_get(row_key, 'stats:num_times_seen')
        if not tot_ims:
            return 0.
        return float(tot_seen) / tot_ims


    def get_n_images(self, n, image_attributes=IMAGE_ATTRIBUTES,
                     is_practice=False):
        """
        Randomly samples n active images from the database and returns their
        IDs in accordance with their sampling surplus (or not, if it's a
        practice)

        :param n: Number of images to choose.
        :param image_attributes: The image attributes that the images return
                                 must satisfy.
        :param is_practice: Boolean indicating whether or not the images are
                            for a practice or a real task. If its for a
                            practice, it will ignore the sampling deficit.
        :return: A list of image IDs, unless it cannot get enough images --
                 then returns None.
        """
        table = self.conn.table(IMAGE_TABLE)
        count = self.get_n_active_images_count(
            image_attributes=image_attributes)
        if n > count:
            _log.warning('Insufficient number of active images, '
                         'activating %i more.' % ACTIVATION_CHUNK_SIZE)
            return None
        generator = \
            self.get_active_image_scanner(image_attributes=image_attributes)
        prob = float(n) / count
        images = set()
        while len(images) < n:
            im_id = generator.next()
            if np.random.rand() > prob:
                continue
            if is_practice:
                images.add(im_id)
            sd = table.counter_get(im_id, 'stats:sampling_surplus')
            if sd <= 0:
                images.add(im_id)
            else:
                table.counter_dec(im_id, 'stats:sampling_surplus')
        return list(images)

    # TASK DESIGN STUFF
    
    def gen_design(self, n, t, j, image_attributes=IMAGE_ATTRIBUTES):
        """
        Returns a task design, as a series of tuples of images. This is based
        directly on generate/utils/get_design, which should be consulted for
        reference on the creation of Steiner systems.
    
        This extends get_design by not only checking against co-occurrence
        within the task, but also globally across all tasks by invoking
        _tuple_permitted.

        :param n: The number of distinct elements involved in the experiment.
        :param t: The number of elements to present each trial.
        :param j: The number of times each element should appear during the
                  experiment.
        :param image_attributes: The attributes that images must have to be
                                 into the study. Images must have any of
                                 these attributes.
        :return: A list of tuples representing each subset. Elements may be
                 randomized within trial and subset order may be randomized
                 without consequence. If there is not enough images to
                 generate, returns None.
        """
        occ = np.zeros(n)  # an array which stores the number of times an
        # image has been used.
        design = []
        images = self.get_n_images(n, image_attributes=image_attributes)
        if images is None:
            _log.error('Unable to fetch images to generate design!')
            return None
        # shuffle the images (remember its in-place! >.<)
        np.random.shuffle(images)
        # the set of observed tuples
        obs = _get_preexisting_pairs(self.conn, images)
        for iocc in range(0, t + j):
            # maximize the efficiency of the design, and also ensure that the
            #  number of j-violations (the number of times an image is shown
            # over the whole task - j) is less than or equal to t.
            for c in comb(range(n), t):
                if np.min(occ) == j:
                    return design  # you're done
                cvio = 0  # the count of current violations
                # check the candidate tuple
                cur_tuple = tuple([images[x] for x in c])
                if not _tuple_permitted(cur_tuple, obs):
                    continue
                occ_arr = occ[list(c)]
                if max(occ_arr) > iocc:
                    # check that the image hasn't occured too many times for
                    # this iteration.
                    continue
                if min(occ_arr) >= j:
                    # make sure that at least one of these images even needs
                    # to be shown!
                    continue
                # ug, I was storing observed image indices instead of the
                # keys. I'm an idiot.
                for x1, x2 in comb(cur_tuple, 2):
                    obs.add(pair_to_tuple(x1, x2))
                for i in c:
                    occ[i] += 1
                design.append(cur_tuple)
        if not np.min(occ) >= j:
            import ipdb
            ipdb.set_trace()
            _log.warning('Could not generate design.')
            return None
        return design

    def gen_task(self, n, t, j, n_keep_blocks=None, n_reject_blocks=None,
                 prompt=None, practice=False, attribute=ATTRIBUTE,
                 random_segment_order=RANDOMIZE_SEGMENT_ORDER,
                 image_attributes=IMAGE_ATTRIBUTES, hit_type_id=None):
        """
        Creates a new task, by calling gen_design and then arranging those
        tuples into keep and reject blocks.

        Additionally, you may specify which hit_type_id this task should be
        for. If this is the case, it overwrites:
            task_attribute
            image_attributes
            practice
        In accordance with the design philosophy of segregating function,
        gen_task does not attempt to modify the database. Instead, it returns
        elements that befit a call to Set's register_task.
    
        NOTES:
            Keep blocks always come first, after which they alternate between
            Keep / Reject. If the RANDOMIZE_SEGMENT_ORDER option is true,
            then the segments order will be randomized.
    
            The randomization has to be imposed here, along with all other
            order decisions, because the database assumes that data from
            mechanical turk (i.e., as determined by the task HTML) are in the
            same order as the data in the database.

            This does NOT register the task. It returns a dictionary that
            befits dbset, but does not do it itself.

            In order to check for contradictions given the fact that the
            tuple order and the within-tuple image order is randomized,
            this function also establishes a mapping from tuples to invariant
            indices as well as a mapping from the images within a tuple to a
            similarly invariant mapping.

            N.B. "global_image_idx_map" is not truly "global" -- it is global
            only up to the current task. The mapping mapping does not extend
            to the global set of ALL images, of course.

        :param n: The number of distinct elements involved in the experiment.
        :param t: The number of elements to present each trial.
        :param j: The number of times each element should appear during the
                  experiment.
        :param n_keep_blocks: The number of keep blocks in this task (tuples
                              are evenly divided among them)
        :param n_reject_blocks: The number of reject blocks in this task (
                                tuples are evenly divided among them)
        :param prompt: The prompt to use across all blocks (overrides defaults)
        :param practice: Boolean, whether or not this task is a practice.
        :param attribute: The task attribute.
        :param random_segment_order: Whether or not to randomize block ordering.
        :param image_attributes: The set of attributes that the images from
                                 this task have.
        :param hit_type_id: The HIT type ID, as provided by MTurk and as
                            findable in the database.
        :return: task_id, exp_seq, attribute, register_task_kwargs. On
                 failure, returns None.
        """
        if practice:
            task_id = practice_id_gen()
        else:
            task_id = task_id_gen()
        if hit_type_id:
            hit_type_info = self.get_hit_type_info(hit_type_id)
            practice = hit_type_info.get('metadata:is_practice', FALSE) == TRUE
            attribute = hit_type_info.get('metadata:attribute', ATTRIBUTE)
            image_attributes = list(loads(hit_type_info.get(
                'metadata:image_attributes', dumps(IMAGE_ATTRIBUTES))))
        if n_keep_blocks is None:
            if practice:
                n_keep_blocks = DEF_PRACTICE_KEEP_BLOCKS
            else:
                n_keep_blocks = DEF_KEEP_BLOCKS
        if n_reject_blocks is None:
            if practice:
                n_reject_blocks = DEF_PRACTICE_REJECT_BLOCKS
            else:
                n_reject_blocks = DEF_REJECT_BLOCKS
        if prompt is None:
            if practice:
                prompt = DEF_PRACTICE_PROMPT
            else:
                prompt = DEF_PROMPT
        # get the tuples
        image_tuples = self.gen_design(n, t, j,
                                       image_attributes=image_attributes)
        if image_tuples is None:
            return None, None, None, None
        # assemble a dict mapping image_tuples images to an index
        global_image_idx_map = dict()
        for image_tuple in image_tuples:
            for image in image_tuple:
                if image not in global_image_idx_map:
                    global_image_idx_map[image] = len(global_image_idx_map)
        # arrange them into blocks
        keep_shuf_tups, keep_shuf_idxs = _shuffle_tuples(image_tuples)
        keep_tuples = [x for x in chunks(keep_shuf_tups, n_keep_blocks)]
        keep_idxs = [x for x in chunks(keep_shuf_idxs, n_keep_blocks)]
        rej_shuf_tups, rej_shuf_idxs = _shuffle_tuples(image_tuples)
        reject_tuples = [x for x in chunks(rej_shuf_tups, n_reject_blocks)]
        reject_idxs = [x for x in chunks(rej_shuf_idxs, n_reject_blocks)]
        keep_blocks = []
        reject_blocks = []
        for kt, kt_idxs in zip(keep_tuples, keep_idxs):
            block = dict()
            block['images'] = [list(x) for x in kt]
            block['image_idx_map'] = [[global_image_idx_map[y] for y in x]
                                      for x in kt]
            block['type'] = KEEP_BLOCK
            block['instructions'] = DEF_KEEP_BLOCK_INSTRUCTIONS
            block['prompt'] = prompt
            block['global_tup_idxs'] = kt_idxs
            keep_blocks.append(block)
        for rt, rt_idxs in zip(reject_tuples, reject_idxs):
            block = dict()
            block['images'] = [list(x) for x in rt]
            block['image_idx_map'] = [[global_image_idx_map[y] for y in x]
                                      for x in rt]
            block['type'] = REJECT_BLOCK
            block['instructions'] = DEF_REJECT_BLOCK_INSTRUCTIONS
            block['prompt'] = prompt
            block['global_tup_idxs'] = rt_idxs
            reject_blocks.append(block)
        blocks = []
        while len(keep_blocks) or len(reject_blocks):
            if len(keep_blocks):
                blocks.append(keep_blocks.pop(0))
            if len(reject_blocks):
                blocks.append(reject_blocks.pop(0))
        if random_segment_order:
            np.random.shuffle(blocks)
        # define expSeq
        # annoying expSeq expects image tuples...
        exp_seq = [[x['type'], [tuple(y) for y in x['images']]] for x in blocks]
        register_task_kwargs = {'blocks': blocks, 'is_practice': practice,
                                'check_ims': True,
                                'image_attributes': image_attributes}
        return task_id, exp_seq, attribute, register_task_kwargs

    def get_active_hit_type_id_for_task(self, task_id):
        """
        Returns the ID for an appropriate HIT type given the task. This is
        potentially expensive, but will be done offline.

        :param task_id: The task ID, as a string.
        :return: An appropriate HIT type ID for this task, otherwise None.
        Returns the first one it finds.
        """
        task_info = self.conn.table(TASK_TABLE).row(task_id)
        cur_task_is_practice = task_info.get('metadata:is_practice', FALSE) \
                               == TRUE
        task_attribute = task_info.get('metadata:attribute', ATTRIBUTE)
        image_attributes = loads(task_info.get('metadata:image_attributes',
                                               dumps(IMAGE_ATTRIBUTES)))
        if cur_task_is_practice:
            scanner = self.get_active_practice_hit_types()
        else:
            scanner = self.get_active_hit_types()
        for hit_type_id, _ in scanner:
            if self.hit_type_matches(hit_type_id, task_attribute,
                                     image_attributes):
                return hit_type_id
        return None

    def worker_autoban_check(self, worker_id, duration=None):
        """
        Checks that the worker should be autobanned.

        :param conn: The HappyBase connection object.
        :param worker_id: The worker ID, as a string.
        :return: True if the worker should be autobanned, False otherwise.
        """
        if self.worker_weekly_rejected(worker_id) > MIN_REJECT_AUTOBAN_ELIGIBLE:
            if self.worker_weekly_reject_accept_ratio(self, worker_id) > \
                    AUTOBAN_REJECT_ACCEPT_RATIO:
                return True
        return False


"""
Main Classes - SET
"""


class Set(object):
    """
    Handles all update events for the database. The following events are
    possible, which loosely fall into groups:

    Group I
    New task to be registered.
    New worker to be registered.
    New image(s) to be registered.

    Group II
    A practice has been served to a worker.
    A task has been served to a worker.

    Group III
    A worker's demographic information has to be logged.

    Group IV
    A worker has finished a task.
    A worker has passed a practice.
    A worker has failed a practice.

    Group V
    A task has been accepted.
    A task has been rejected.

    Group VI
    Image(s) to be activated.

    Group VII
    Add legacy worker - done via register_worker
    Add legacy task
    Add legacy pair - done via register_pair
    Add legacy win

    Group VIII
    Create/recreate workers table
    Create/recreate tasks table
    Create/recreate images table
    Create/recreate pairs table
    Create/recreate wins table
    """
    def __init__(self, conn):
        """
        :param conn: The HappyBase / HBase connection object.
        :return: A Get instance.
        """
        self.conn = conn

    def _image_is_active(self, image_id):
        """
        Returns True if an image has been registered into the database and is
        an active image.

        NOTES:
            Private version of Get method, for use internally for methods of
            Set.

        :param image_id: The image ID, which is the row key.
        :return: True if the image is active. False otherwise.
        """
        table = self.conn.table(IMAGE_TABLE)
        is_active = table.row(image_id, columns=['metadata:is_active']).get(
            'metadata:is_active', None)
        if is_active == TRUE:
            return True
        else:
            return False

    @staticmethod
    def _table_has_row(table, row_key):
        """
        Determines if a table has a defined row key or not.

        NOTES:
            Private version of Get method, for use internally for methods of
            Set.

        :param table: A HappyBase table object.
        :param row_key: The desired row key, as a string.
        :return: True if key exists, false otherwise.
        """
        scan = table.scan(row_start=row_key, filter='KeyOnlyFilter() AND '
                                                    'FirstKeyOnlyFilter()',
                          limit=1)
        return next(scan, None) is not None

    def _get_task_status(self, task_id):
        """
        Fetches the status code given a task ID.

        NOTES:
            Private version of Get method, for use internally for methods of
            Set.

        :param task_id: The task ID, which is the row key.
        :return: A status code, as defined in conf.
        """
        table = self.conn.table(TASK_TABLE)
        if not self._table_has_row(table, task_id):
            return DOES_NOT_EXIST
        task = table.row(task_id)
        if task.get('metadata:is_practice', FALSE) == TRUE:
            return IS_PRACTICE
        if task.get('status:awaiting_serve', FALSE) == TRUE:
            if task.get('status:awaiting_hit_group', FALSE) == TRUE:
                return AWAITING_HIT
            return AWAITING_SERVE
        if task.get('status:pending_completion', FALSE) == TRUE:
            return COMPLETION_PENDING
        if task.get('status:pending_evaluation', FALSE) == TRUE:
            return EVALUATION_PENDING
        if task.get('status:accepted', FALSE) == TRUE:
            return ACCEPTED
        if task.get('status:rejected', FALSE) == TRUE:
            return REJECTED
        return UNKNOWN_STATUS

    def create_worker_table(self, clobber=False):
        """
        Creates a workers table, with names based on conf.

        :param clobber: Boolean, if true will erase old workers table if it
                        exists. [def: False]
        :return: True if table was created. False otherwise.
        """
        _log.info('Creating worker table.')
        return _create_table(self.conn, WORKER_TABLE, WORKER_FAMILIES, clobber)

    def create_task_table(self, clobber=False):
        """
        Creates a tasks table, with names based on conf.

        :param clobber: Boolean, if true will erase old tasks table if it
               exists. [def: False]
        :return: True if table was created. False otherwise.
        """
        _log.info('Creating task table.')
        return _create_table(self.conn, TASK_TABLE, TASK_FAMILIES, clobber)

    def create_image_table(self, clobber=False):
        """
        Creates a images table, with names based on conf.

        :param clobber: Boolean, if true will erase old tasks table if it
               exists. [def: False]
        :return: True if table was created. False otherwise.
        """
        _log.info('Creating image table.')
        return _create_table(self.conn, IMAGE_TABLE, IMAGE_FAMILIES, clobber)

    def create_pair_table(self, clobber=False):
        """
        Creates a pairs table, with names based on conf.

        :param clobber: Boolean, if true will erase old tasks table if it
               exists. [def: False]
        :return: True if table was created. False otherwise.
        """
        _log.info('Creating pair table.')
        return _create_table(self.conn, PAIR_TABLE, PAIR_FAMILIES, clobber)

    def create_win_table(self, clobber=False):
        """
        Creates a wins table, with names based on conf.

        :param clobber: Boolean, if true will erase old tasks table if it
               exists. [def: False]
        :return: True if table was created. False otherwise.
        """
        _log.info('Creating win table.')
        return _create_table(self.conn, WIN_TABLE, WIN_FAMILIES, clobber)

    def create_task_type_table(self, clobber=False):
        """
        Creates a HIT type table, that stores information about HIT types.

        :param clobber: Boolean, if true will erase old tasks table if it
               exists. [def: False]
        :return: True if table was created. False otherwise.
        """
        _log.info('Creating HIT type table')
        return _create_table(self.conn, HIT_TYPE_TABLE, HIT_TYPE_FAMILIES,
                             clobber)

    def force_regen_tables(self):
        """
        Forcibly rebuilds all tables.

        WARNING: DO NOT USE THIS LIGHTLY!

        :return: True if the tables were all successfully regenerated.
        """
        succ = True
        succ = succ and self.create_worker_table(clobber=True)
        succ = succ and self.create_image_table(clobber=True)
        succ = succ and self.create_pair_table(clobber=True)
        succ = succ and self.create_task_table(clobber=True)
        succ = succ and self.create_win_table(clobber=True)
        succ = succ and self.create_task_type_table(clobber=True)
        return succ

    """
    ADDING / CHANGING DATA
    """

    def register_hit_type(self, hit_type_id, task_attribute=ATTRIBUTE,
                          image_attributes=IMAGE_ATTRIBUTES,
                          title=DEFAULT_TASK_NAME, description=DESCRIPTION,
                          reward=DEFAULT_TASK_PAYMENT,
                          assignment_duration=HIT_TYPE_DURATION,
                          keywords=KEYWORDS,
                          auto_approve_delay=AUTO_APPROVE_DELAY,
                          is_practice=False, active=True):
        """
        Registers a HIT type in the database.

        :param hit_type_id: The HIT type ID, as provided by mturk (see
                            webserver.mturk.register_hit_type_mturk).
        :param task_attribute: The task attribute for tasks that are HITs
                               assigned to this HIT type.
        :param image_attributes: The image attributes for tasks that are HITs
                                 assigned to this HIT type.
        :param title: The HIT Type title.
        :param description: The HIT Type description.
        :param reward: The reward for completing this type of HIT.
        :param assignment_duration: How long this HIT Type persists for.
        :param keywords: The HIT type keywords.
        :param auto_approve_delay: The auto-approve delay.
        :param is_practice: Boolean, or FALSE/TRUE (see conf). Whether or not
                            this HIT type should be used for practice tasks
                            (remember that they are mutually exclusive; no
                            hit type should be used for both practice and
                            authentic/'real' tasks.)
        :param active: Boolean, or FALSE/TRUE (see conf). Whether or not this
                       HIT is active, i.e., if new HITs / Tasks should be
                       assigned to this HIT type.
        :return: None.
        """
        _log.info('Registering HIT Type %s' % hit_type_id)
        if (type(active) is not bool) and (active is not FALSE and active is
        not TRUE):
            _log.warning('Unknown active status, defaulting to FALSE')
            active = FALSE
        if (type(is_practice) is not bool) and (is_practice is not FALSE and
                                                        is_practice is not
                                                        TRUE):
            _log.warning('Unknown practice status, defaulting to FALSE')
            is_practice = FALSE
        hit_type_dict = {'metadata:task_attribute': task_attribute,
                         'metadata:title': title,
                         'metadata:image_attributes': dumps(set(
                             image_attributes)),
                         'metadata:description': description,
                         'metadata:reward': reward,
                         'metadata:assignment_duration': assignment_duration,
                         'metadata:keywords': keywords,
                         'metadata:auto_approve_delay': auto_approve_delay,
                         'metadata:is_practice': is_practice,
                         'status:active': active}
        table = self.conn.table(HIT_TYPE_TABLE)
        table.put(hit_type_id, _conv_dict_vals(hit_type_dict))

    def register_task(self, task_id, exp_seq, attribute, blocks=None,
                      is_practice=False, check_ims=False, image_attributes=[]):
        """
        Registers a new task to the database.

        NOTES:
            exp_seq takes the following format:
                [segment1, segment2, ...]
            where segmentN is:
                [type, [image tuples]]
            where type is either keep or reject and tuples are:
                [(image1-1, ..., image1-M), ..., (imageN-1, ..., imageN-M)]

            Since we don't need to check up on these values all that often,
            we will be converting them to strings using dill.

            Because tasks may expire and need to be reposted as a new HIT or
            somesuch, do not provide any information about the HIT_ID,
            the Task type ID, etc--in other words, no MTurk-specific
            information. At this point in the flow of information,
            our knowledge about the task is constrained to be purely local
            information.

        :param task_id: The task ID, as a string.
        :param exp_seq: A list of lists, in order of presentation, one for
                        each segment. See Notes.
        :param attribute: The image attribute this task pertains to, e.g.,
                          'interesting.'
        :param blocks: The experimental blocks (fit for being placed into
                       generate.make_html)
        :param is_practice: A boolean, indicating if this task is a practice
                            or not. [def: False]
        :param check_ims: A boolean. If True, it will check that every image
                          required is in the database.
        :param image_attributes: The set of attributes that the images from
                                 this task have.
        :return: None.
        """

        _log.info('Registering task %s' % task_id)
        # determine the total number of images in this task.
        task_dict = dict()
        pair_list = set()  # set of pairs
        task_dict['metadata:is_practice'] = is_practice
        task_dict['metadata:attribute'] = attribute
        images = set()
        image_list = [] # we also need to store the images as a list in case
        # some need to be incremented more than once
        im_tuples = []
        im_tuple_types = []
        table = self.conn.table(IMAGE_TABLE)
        for seg_type, segment in exp_seq:
            for im_tuple in segment:
                for im in im_tuple:
                    if check_ims:
                        if not self._image_is_active(im):
                            _log.warning('Image %s is not active or does not '
                                         'exist.' % im)
                            continue
                    images.add(im)
                    image_list.append(im)
                for imPair in comb(im_tuple, 2):
                    pair_list.add(tuple(sorted(imPair)))
                im_tuples.append(im_tuple)
                im_tuple_types.append(seg_type)
        if not is_practice:
            for img in image_list:
                table.counter_inc(img, 'stats:num_times_seen')
        # note: not in order of presentation!
        task_dict['metadata:images'] = dumps(images)
        task_dict['metadata:tuples'] = dumps(im_tuples)
        task_dict['metadata:tuple_types'] = dumps(im_tuple_types)
        task_dict['metadata:attributes'] = dumps(set(image_attributes))
        task_dict['status:awaiting_serve'] = TRUE
        task_dict['status:awaiting_hit_type'] = TRUE
        if blocks is None:
            _log.error('No block structure defined for this task - will not '
                       'be able to load it.')
        else:
            task_dict['blocks:c1'] = dumps(blocks)
        # Input the data for the task table
        table = self.conn.table(TASK_TABLE)
        table.put(task_id, _conv_dict_vals(task_dict))
        # Input the data for the pair table.
        if is_practice and not STORE_PRACTICE_PAIRS:
            return
        table = self.conn.table(PAIR_TABLE)
        b = table.batch()
        for pair in pair_list:
            pid = _get_pair_key(pair[0], pair[1])
            b.put(pid, _get_pair_dict(pair[0], pair[1], task_id, attribute))
        b.send()

    def deactivate_hit_type(self, hit_type_id):
        """
        Deactivates a HIT type, so that it is no longer accepting new tasks /
        HITs.

        :param hit_type_id: The HIT type ID, as provided by mturk.
        :return: None
        """
        _log.info('Deactivating HIT Type %s' % hit_type_id)
        table = self.conn.table(HIT_TYPE_TABLE)
        if not self._table_has_row(table, hit_type_id):
            _log.warn('No such HIT type: %s' % hit_type_id)
            return
        table.put(hit_type_id, {'status:active': FALSE})

    def indicate_task_has_hit_type(self, task_id):
        """
        Sets status:awaiting_hit_type parameter of the task, indicating that
        it has been added to a HIT type

        :param task_id: The task ID, as a string.
        :return: None
        """
        table = self.conn.table(TASK_TABLE)
        table.put(task_id, {'status:awaiting_hit_type': TRUE})

    def set_task_html(self, task_id, html):
        """
        Stores the task HTML in the database, for future reference.

        :param task_id: The task ID, as a string.
        :param html: The task HTML, as a string. [this might need to be
                     pickled?]
        :return: None
        """
        table = self.conn.table(TASK_TABLE)
        table.put(task_id, {'html:c1': html})

    def register_worker(self, worker_id):
        """
        Registers a new worker to the database.

        NOTES:
            This must be keyed by their Turk ID for things to work properly.

        :param worker_id: A string, the worker ID.
        :return: None.
        """
        _log.info('Registering worker %s' % worker_id)
        table = self.conn.table(WORKER_TABLE)
        if self._table_has_row(table, worker_id):
            _log.warning('User %s already exists, aborting.' % worker_id)
        table.put(worker_id, {'status:passed_practice': FALSE,
                              'status:is_legacy': FALSE,
                              'status:is_banned': FALSE,
                              'status:random_seed': str(int((datetime.now(

                              )-datetime(2016, 1, 1)).total_seconds()))})

    def register_images(self, image_ids, image_urls, attributes=[]):
        """
        Registers one or more images to the database.

        :param image_ids: A list of strings, the image IDs.
        :param image_urls: A list of strings, the image URLs (in the same
                           order as image_ids).
        :param attributes: The image attributes. This will allow us to run
                           separate experiments on different subsets of  the
                           available images. This should be a list of
                           strings, such as "people." They are set to True here.
        :return: None.
        """
        # get the table
        _log.info('Registering %i images.'%(len(image_ids)))
        table = self.conn.table(IMAGE_TABLE)
        b = table.batch()
        if type(attributes) is str:
            attributes = [attributes]
        for iid, iurl in zip(image_ids, image_urls):
            imdict = _get_image_dict(iurl)
            if imdict is None:
                continue
            for attribute in attributes:
                imdict['attributes:%s' % attribute] = TRUE
            b.put(iid, imdict)
        b.send()

    def add_attributes_to_images(self, image_ids, attributes):
        """
        Adds attributes to the requested images.

        :param image_ids: A list of strings, the image IDs.
        :param attributes: The image attributes, as a list. See register_images
        :return: None
        """
        if type(image_ids) is str:
            image_ids = [image_ids]
        if type(attributes) is str:
            attributes = [attributes]
        _log.info('Adding %i attributes to %i images.'%(len(attributes),
                                                        len(image_ids)))
        table = self.conn.table(IMAGE_TABLE)
        b = table.batch()
        for iid in image_ids:
            up_dict = dict()
            for attribute in attributes:
                up_dict['attributes:%s' % attribute] = TRUE
            b.put(iid, up_dict)
        b.send()

    def _reset_sampling_counts(self):
        """
        In order to ensure that the comparison graph is an Erdos-Renyi random
        graph, the probability of any two nodes having an edge must be
        independent. However, we induce a dependency by growing the graph
        over time, i.e., with activate_images(.).

        To ameliorate this, the function defined here resets the sampling
        counts such that an image sampled n times before will not be sampled
        again until it has been chosen again n times. Hence, if the
        time_sampled value was n before calling this function, it becomes -n
        after and the image will only be resampled after the value is >= 0

        :return: None.
        """
        _log.info('Resetting sampling counts')
        table = self.conn.table(IMAGE_TABLE)
        scanner = table.scan(columns=['metadata:is_active'],
                             filter=attribute_image_filter(only_active=True))
        for im_key, _ in scanner:
            cur_im_sample_surplus = \
                table.counter_get(im_key, 'stats:num_times_seen')
            table.counter_set(im_key,
                              'stats:sampling_surplus',
                              cur_im_sample_surplus)

    def activate_images(self, image_ids):
        """
        Activates some number of images, i.e., makes them available for tasks.

        :param image_ids: A list of strings, the image IDs.
        :return: None.
        """
        _log.info('Activating %i images.' % len(image_ids))
        table = self.conn.table(IMAGE_TABLE)
        b = table.batch()
        for iid in image_ids:
            if not self._table_has_row(table, iid):
                _log.warning('No data for image %s'%(iid))
                continue
            b.put(iid, {'metadata:is_active': TRUE})
        b.send()
        self._reset_sampling_counts()

    def activate_n_images(self, n, image_attributes=IMAGE_ATTRIBUTES):
        """
        Activates N new images.

        :param n: The number of new images to activate.
        :param image_attributes: The attributes of the images we are selecting.
        :return: None.
        """
        table = self.conn.table(IMAGE_TABLE)
        scanner = table.scan(columns=['metadata:is_active'],
                             filter=attribute_image_filter(image_attributes,
                                                           only_inactive=True))
        to_activate = []
        for row_key, rowData in scanner:
            to_activate.append(row_key)
            if len(to_activate) == n:
                break
        self.activate_images(to_activate)

    def practice_served(self, task_id, worker_id):
        """
        Notes that a practice has been served to a worker.

        :param task_id: The ID of the practice served.
        :param worker_id: The ID of the worker to whom the practice was served.
        :return: None.
        """
        _log.info('Serving practice %s to worker %s' % (task_id, worker_id))
        table = self.conn.table(WORKER_TABLE)
        table.counter_inc(worker_id, 'stats:num_practices_attempted')
        table.counter_inc(worker_id, 'stats:num_practices_attempted_interval')

    def task_served(self, task_id, worker_id, hit_id=None, hit_type_id=None,
                    payment=None):
        """
        Notes that a task has been served to a worker.

        :param task_id: The ID of the task served.
        :param worker_id: The ID of the worker to whom the task was served.
        :param hit_id: The MTurk HIT ID, if known.
        :param hit_type_id: The hash of the task attribute and the image
                            attributes, as produced by
                            webserver.mturk.get_hit_type_id
        :param payment: The task payment, if known.
        :return: None.
        """
        _log.info('Serving task %s served to %s' % (task_id, worker_id))
        table = self.conn.table(TASK_TABLE)
        table.put(task_id, _conv_dict_vals({'metadata:worker_id': worker_id,
                                            'metadata:hit_id': hit_id,
                                            'metadata:hit_type_id': hit_type_id,
                                            'metadata:payment': payment,
                                            'status:pending_completion': TRUE,
                                            'status:awaiting_serve': FALSE}))
        table = self.conn.table(WORKER_TABLE)
        # increment the number of incomplete trials for this worker.
        table.counter_inc(worker_id, 'stats:num_incomplete')
        table.counter_inc(worker_id, 'stats:numAttempted')
        table.counter_inc(worker_id, 'stats:numAttemptedThisWeek')

    def worker_demographics(self, worker_id, gender, birthyear):
        """
        Sets a worker's demographic information.

        :param worker_id: The ID of the worker.
        :param gender: Worker gender.
        :param birthyear: Worker year of birth.
        :return: None
        """
        _log.info('Saving demographics for worker %s'% worker_id)
        table = self.conn.table(WORKER_TABLE)
        table.put(worker_id,
                  _conv_dict_vals({'demographics:birthyear': birthyear,
                                   'demographics:gender': gender}))

    def task_finished_from_json(self, resp_json, user_agent=None,
                                hit_type_id=None):
        """
        Indicates a task is finished and stores the response data from a json
        request object. The HIT Type ID and the worker IP address are the only
        things that cannot be fetched from the json, and hence has to be
        provided separately.

        NOTES:
            This function may have to change if the response JSON structure
            changes significantly; however, this is considered sufficiently
            unlikely to justify a relatively low-level function that
            interacts directly with the database.

        :param resp_json: The response JSON, from a MTurk task using jsPsych
        :param hit_type_id: The HIT Type ID.
        :param user_agent: A Flask user-agent object.
        :return: The fraction of missed trials and contradictions as well as
                 the chisquare pval.
        """
        worker_id = resp_json[0]['workerId']
        hit_id = resp_json[0]['hitId']
        task_id = resp_json[0]['taskId']
        assignment_id = resp_json[0]['assignmentId']
        # extract the experimental blocks
        data = filter(lambda x: x['trial_type'] == 'click-choice', resp_json)
        choices = []
        rts = []
        choice_idxs = []
        actions = []
        contradiction_dict = dict()
        for block in data:
            choices.append(block.get('choice', -1))
            rts.append(block.get('rt', -1))
            choice_idxs.append(block.get('choice_idx', -1))
            actions.append(block.get('action_type', -1))
            global_tup_idx = block.get('global_tup_idx', None)
            if (block.get('choice', -1) != -1 and
                        block.get('choice_idx', -1) != -1):
                # if the choice was made, see if it was contradictory
                taskwide_im_idx = block['image_idx_map'][block['choice_idx']]
                if global_tup_idx is not None:
                    contradiction_dict[global_tup_idx] = (
                        contradiction_dict.get(global_tup_idx, []) +
                                                          [taskwide_im_idx])
        # compute the number unanswered
        num_unanswered = sum([x == -1 for x in choices])
        # compute the number of contradictory statements
        num_contradictions = 0
        for key in contradiction_dict:
            if len(contradiction_dict[key]) > 1:
                if contradiction_dict[key][0] == contradiction_dict[key][1]:
                    num_contradictions += 1
        # compute the distribution of clicks.
        total_observations = 0
        max_index = 0
        counts_by_index = Counter()
        for idx in choice_idxs:
            if idx > -1:
                counts_by_index[idx] += 1
                total_observations += 1
                if max_index < idx:
                    max_index = idx
            # compute the p value
        obs = [float(counts_by_index[key]) / total_observations for key in
               range(max_index)]
        expected = [float(total_observations) / max_index for _ in range(
            max_index)]
        chi_stat, p_value = stats.chisquare(obs, expected)
        frac_contradictions = float(num_contradictions) / len(data)
        frac_unanswered = float(num_unanswered) / len(data)
        mean_rt = np.mean(filter(lambda x: x > -1, rts))
        table = self.conn.table(TASK_TABLE)
        import ipdb
        ipdb.set_trace()
        input_dict = {'metadata:worker_id': worker_id,
                      'metadata:hit_id': hit_id,
                      'metadata:assignment_id': assignment_id,
                      'completion_data:choices': dumps(choices),
                      'completion_data:action': dumps(actions),
                      'completion_data:reaction_times': dumps(rts),
                      'completion_data:response_json': json.dumps(
                          resp_json),
                      'metadata:hit_type_id': str(hit_type_id),
                      'validation_statistics:prob_random': '%.4f' % p_value,
                      'validation_statistics:frac_contradictions':
                          '%.4f' % frac_contradictions,
                      'validation_statistics:frac_no_response':
                          '%.4f' % frac_unanswered,
                      'validation_statistics:mean_rt': '%.4f' % mean_rt}
        try:
            table.put(task_id, **_conv_dict_vals(input_dict))
        except:
            import dill
            with open('/repos/mturk_task_v2/dumped_registration_inputs',
                      'w') as f:
                dill.dump([resp_json, user_agent, hit_type_id], f)
        if user_agent is not None:
            table.put(task_id, _conv_dict_vals(
                                {'user_agent:browser': user_agent.browser,
                                 'user_agent:language': user_agent.language,
                                 'user_agent:platform': user_agent.platform,
                                 'user_agent:version': user_agent.version,
                                 'user_agent:string': user_agent.string}))
        return frac_contradictions, frac_unanswered, mean_rt, p_value

    def validate_task(self, task_id=None, frac_contradictions=None,
                      frac_unanswered=None, mean_rt=None,
                      prob_random=None):
        """
        Validates a task, either by providing it with the task id or the
        numbers themselves. Violating any one of the constraints established
        by the TASK_VALIDATION section in conf.py results in the data being
        discarded.

        :param task_id: The task ID, as a string.
        :param frac_contradictions: The fraction of contradictory selections
                                    in the task.
        :param frac_unanswered: The fraction of missed selections in the task.
        :param mean_rt: The average reaction time, in milliseconds.
        :param prob_random: The Chisquare distribution p-value for whether or
                            not this individual behaved randomly. In other
                            words, it assesses the probability that they were
                            clicking on images purely based on content (as
                            they should be)
        :return: A boolean indicating whether or not this task is acceptable
                 as well as a reason for the rejection (if it is
                 unacceptable) or None.
        """
        table = self.conn.table(TASK_TABLE)

        def validate_rt(task_id, mean_rt):
            """validates based on reaction time"""
            if mean_rt is None:
                if task_id is None:
                    _log.warning('Average reaction time not provided nor is '
                                 'task_id. Task will not be judged on '
                                 'reaction time.')
                    return True, None
                else:
                    try:
                        mean_rt_str = float(table.row(task_id).get(
                            'validation_statistics:mean_rt', None))
                    except TypeError:
                        _log.warning(('Could not acquire mean_rt for task %s. '
                                      'Task will not be judged on reaction '
                                      'time') % task_id)
                        return True, None
                    mean_rt = float(mean_rt_str)
            if mean_rt < MIN_MEAN_RT:
                return False, BAD_DATA_TOO_FAST
            if mean_rt > MAX_MEAN_RT:
                return False, BAD_DATA_TOO_SLOW
            return True, None

        def validate_frac_unanswered(task_id, frac_unanswered):
            """validates based on the fraction unanswered."""
            if frac_unanswered is None:
                if task_id is None:
                    _log.warning('Fraction of unanswered not provided nor is '
                                 'task_id. Task will not be judged on the '
                                 'fraction unanswered.')
                    return True, None
                else:
                    try:
                        frac_unanswered_str = float(table.row(task_id).get(
                            'validation_statistics:frac_no_response', None))
                    except TypeError:
                        _log.warning(('Could not acquire frac_no_response for '
                                      'task %s. Task will not be judged on the '
                                      'fraction unanswered') % task_id)
                        return True, None
                    frac_unanswered = float(frac_unanswered_str)
            if frac_unanswered > MAX_FRAC_UNANSWERED:
                return False, BAD_DATA_TOO_MANY_UNANSWERED
            return True, None

        def validate_frac_contradictions(task_id, frac_contradictions):
            """validates based on the fraction of contradictions."""
            if frac_contradictions is None:
                if task_id is None:
                    _log.warning('Fraction of contradictions not provided nor'
                                 ' is task_id. Task will not be judged on the '
                                 'fraction of contradictions.')
                    return True, None
                else:
                    try:
                        frac_contradictions_str = float(
                            table.row(task_id).get(
                                'validation_statistics:frac_contradictions',
                                None))
                    except TypeError:
                        _log.warning(('Could not acquire frac_contradictions '
                                      'for task %s. '
                                      'Task will not be judged on the '
                                      'fraction of contradictions') % task_id)
                        return True, None
                    frac_contradictions = float(frac_contradictions_str)
            if frac_contradictions > MAX_FRAC_CONTRADICTIONS:
                return False, BAD_DATA_TOO_CONTRADICTORY
            return True, None

        def validate_prob_random(task_id, prob_random):
            """validates based on the probability that they are behaving
            randomly."""
            if prob_random is None:
                if task_id is None:
                    _log.warning('Probability of random behavior not provided '
                                 'nor is task_id. Task will not be judged on '
                                 'the probability of random behavior.')
                    return True, None
                else:
                    try:
                        prob_random_str = float(
                            table.row(task_id).get(
                                'validation_statistics:prob_random', None))
                    except TypeError:
                        _log.warning(('Could not acquire prob_random for task '
                                      '%s. Task will not be judged on the '
                                      'probability of random behavior') %
                                     task_id)
                        return True, None
                    prob_random = float(prob_random_str)
            if prob_random > MAX_PROB_RANDOM:
                return False, BAD_DATA_CLICKING
            return True, None

        val, reason = validate_frac_unanswered(task_id, frac_unanswered)
        if not val:
            return val, reason
        val, reason = validate_rt(task_id, mean_rt)
        if not val:
            return val, reason
        val, reason = validate_prob_random(task_id, prob_random)
        if not val:
            return val, reason
        val, reason = validate_frac_contradictions(task_id, frac_contradictions)
        if not val:
            return val, reason

    def register_demographics(self, resp_json, worker_ip):
        """
        Registers the demographics of a worker.

        :param resp_json: The response JSON of a task from MTurk.
        :param worker_ip: The worker IP address.
        :return: None
        """
        worker_id = resp_json[0]['workerId']
        table = self.conn.table(WORKER_TABLE)
        # note: jsPsych does not provide a means to identify different tasks
        # (say, for instance, by a trial name) hence to find the demographics
        #  trial (if present!) we will have to search through each of them.
        # Guh.
        dem_json = _find_demographics_element_in_json(resp_json)
        if dem_json is None:
            return
        birthyear = dem_json['birthyear']
        gender = dem_json['gender']
        table.put(worker_id, {'demographics:birthyear': str(birthyear),
                              'demographics:gender': str(gender)})
        location_info = geolite2.lookup(worker_ip)
        if location_info is None:
            _log.warn('Could not fetch location info for worker %s' % worker_id)
            return
        table.put(worker_id,
                  **{'location:'+k: str(v) for k, v in location_info.to_dict(
                  ).iteritems()})

    def practice_pass(self, resp_json):
        """
        Notes a pass of a practice task.

        :param resp_json: The response JSON of the practice task that was
                          just passed.
        :return: None.
        """
        table = self.conn.table(WORKER_TABLE)
        # note: jsPsych does not provide a means to identify different tasks
        # (say, for instance, by a trial name) hence to find the demographics
        #  trial (if present!) we will have to search through each of them.
        # Guh.
        worker_id = resp_json[0]['workerId']
        task_id = resp_json[0]['taskId']
        table.put(worker_id, {'status:passed_practice': TRUE,
                              'stats:passed_practice_id': task_id})

    def practice_failure(self, task_id, reason=None):
        """
        Notes a failure of a practice task.

        :param task_id: The ID of the practice to reject.
        :param reason: The reason why the practice was rejected. [def: None]
        :return: None.
        """
        _log.info('Nothing needs to be logged for a practice failure at this '
                  'time.')

    def accept_task(self, task_id):
        """
        Accepts a completed task, updating the worker, task, image, and win
        tables.

        :param task_id: The ID of the task to reject.
        :return: None.
        """
        # update task table, get task data
        _log.info('Task %s has been accepted.' % task_id)
        table = self.conn.table(TASK_TABLE)
        task_status = self._get_task_status(task_id)
        if task_status == DOES_NOT_EXIST:
            _log.error('No such task exists!')
            return
        if task_status != EVALUATION_PENDING:
            _log.warning('Task status indicates it is not ready to be '
                         'accepted, but proceeding anyway')
        task_data = table.row(task_id)
        table.set(task_id, {'status:pending_evaluation': FALSE,
                            'status:accepted': TRUE})
        # update worker table
        worker_id = task_data.get('metadata:worker_id', None)
        if worker_id is None:
            _log.warning('No associated worker for task %s' % task_id)
        table = self.conn.table(WORKER_TABLE)
        # decrement pending evaluation count
        table.counter_dec(worker_id, 'stats:num_pending_eval')
        # increment accepted count
        table.counter_inc(worker_id, 'stats:num_accepted')
        # update images table
        table = self.conn.table(IMAGE_TABLE)
        # unfortunately, happybase does not support batch incrementation (arg!)
        choices = loads(task_data.get('completed_data:choices', None))
        for img in choices:
            table.counter_inc(img, 'stats:num_wins')
        # update the win matrix table
        table = self.conn.table(WIN_TABLE)
        b = table.batch()
        img_tuples = task_data.get('metadata:tuples', None)
        img_tuple_types = task_data.get('metadata:tuple_types', None)
        worker_id = task_data.get('metadata:worker_id', None)
        attribute = task_data.get('metadata:attribute', None)
        # iterate over all the values, and store the data in the win table --
        #  as a batch this will store all the ids that we have to increment (
        # which cant be incremented in a batch)
        ids_to_inc = []
        for ch, tup, tuptype in zip(choices, img_tuples, img_tuple_types):
            if ch == '-1':
                continue
            for img in tup:
                if img != ch:
                    if tuptype.lower() == 'keep':
                        # compute the id for this win element
                        cid = ch + ',' + img
                        ids_to_inc.append(cid)
                        b.put(cid,
                              _conv_dict_vals({'data:winner_id': ch,
                                               'data:loser_id': img,
                                               'data:task_id': task_id,
                                               'data:worker_id': worker_id,
                                               'data:attribute': attribute}))
                    else:
                        cid = img + ',' + ch
                        ids_to_inc.append(cid)
                        b.put(cid,
                              _conv_dict_vals({'data:winner_id': img,
                                               'data:loser_id': ch,
                                               'data:task_id': task_id,
                                               'data:worker_id': worker_id,
                                               'data:attribute': attribute}))
        b.send()
        for cid in ids_to_inc:
            # this increment accounts for legacy shit (uggg)
            table.counter_inc(cid, 'data:win_count')

    def reject_task(self, task_id, reason=None):
        """
        Rejects a completed task.

        :param task_id: The ID of the task to reject.
        :param reason: The reason why the task was rejected. [def: None]
        :return: None.
        """
        # fortunately, not much needs to be done for this.
        # update task table, get task data
        _log.info('Task %s has been rejected.' % task_id)
        table = self.conn.table(TASK_TABLE)
        task_status = self._get_task_status(task_id)
        if task_status == DOES_NOT_EXIST:
            _log.error('No such task exists!')
            return
        if task_status != EVALUATION_PENDING:
            _log.warning('Task status indicates it is not ready to be '
                         'accepted, but proceeding anyway')
        table.set(task_id, _conv_dict_vals({'status:pending_evaluation': FALSE,
                                            'status:rejected': TRUE,
                                            'status:rejection_reason': reason}))
        # update worker table
        task_data = table.row(task_id)
        worker_id = task_data.get('metadata:worker_id', None)
        if worker_id is None:
            _log.warning('No associated worker for task %s' % task_id)
        table = self.conn.table(WORKER_TABLE)
        # decrement pending evaluation count
        table.counter_dec(worker_id, 'stats:num_pending_eval')
        # increment rejected count
        table.counter_inc(worker_id, 'stats:num_rejected')
        table.counter_inc(worker_id, 'stats:num_rejected_interval')

    def reset_worker_counts(self, worker_id):
        """
        Resets the interval counters back to 0, for a particular worker.

        :param worker_id: The worker ID, as a string, as provided by MTurk.
        :return: None.
        """
        table = self.conn.table(WORKER_TABLE)
        table.counter_set(worker_id,
                          'stats:num_practices_attempted_interval',
                          value=0)
        table.counter_set(worker_id,
                          'stats:num_attempted_interval',
                          value=0)
        table.counter_set(worker_id,
                          'stats:num_rejected_interval',
                          value=0)
        table.counter_set(worker_id,
                          'stats:interval_completed_count',
                          value=0)

    def ban_worker(self, worker_id,
                   duration=DEFAULT_BAN_LENGTH,
                   reason=DEFAULT_BAN_REASON):
        """
        Bans a worker for some amount of time.

        :param worker_id: The worker ID, as a string.
        :param duration: The amount of time to ban the worker for, in
                         seconds [default: 1 week]
        :param reason: The reason for the ban.
        :return: None.
        """
        table = self.conn.table(WORKER_TABLE)
        table.put(worker_id, _conv_dict_vals({'status:is_banned': TRUE,
                                              'status:ban_duration': duration,
                                              'status:ban_reason': reason}))

    def worker_ban_expires_in(self, worker_id):
        """
        Checks whether or not a worker's ban has expired; if so, it changes
        the ban status and returns 0. Otherwise, it returns the amount of
        time left in the ban.

        :param worker_id: The worker ID, as a string.
        :return: 0 if the subject is not or is no longer banned, otherwise
                 returns the time until the ban expires.
        """
        table = self.conn.table(WORKER_TABLE)
        data = table.row(worker_id, include_timestamp=True)
        ban_data = data.get('status:is_banned', (FALSE, 0))
        if ban_data[0] == FALSE:
            return 0
        ban_date = time.mktime(time.localtime(float(ban_data[1])/1000))
        cur_date = time.mktime(time.localtime())
        ban_dur = float(data.get('status:ban_length', ('0', 0))[0])
        if (cur_date - ban_date) > ban_dur:
            table.set(worker_id, {'status:is_banned': FALSE,
                                  'status:ban_length': '0'})
            return 0
        else:
            return (cur_date - ban_date) - ban_dur

    def reset_timed_out_tasks(self):
        """
        Checks if a task has been pending for too long without completion; if
        so, it resets it.

        :return: None
        """
        table = self.conn.table(TASK_TABLE)
        to_reset = []  # a list of task IDs to reset.
        scanner = table.scan(columns=['status:pending_completion'],
                             filter=PENDING_COMPLETION_FILTER,
                             include_timestamp=True)
        for row_key, rowData in scanner:
            start_timestamp = rowData.get('status:pending_completion',
                                          (FALSE, '0'))[1]
            start_date = time.mktime(time.localtime(float(
                start_timestamp)/1000))
            cur_date = time.mktime(time.localtime())
            if (cur_date - start_date) > TASK_COMPLETION_TIMEOUT:
                to_reset.append(row_key)
        # Now, un-serve all those tasks
        b = table.batch()
        for task_id in to_reset:
            b.put(task_id, _conv_dict_vals({'metadata:worker_id': '',
                                            'metadata:assignment_id': '',
                                            'metadata:hit_id': '',
                                            'metadata:payment': '',
                                            'status:pending_completion': FALSE,
                                            'status:awaiting_serve': TRUE}))
        b.send()
        _log.info('Found %i incomplete tasks to be reset.' % len(to_reset))

    def deactivate_images(self, image_ids):
        """
        Deactivates a list of images.

        :param image_ids: A list of strings, the image IDs.
        :return: None
        """
        _log.info('Deactivating %i images.' % len(image_ids))
        table = self.conn.table(IMAGE_TABLE)
        b = table.batch()
        for iid in image_ids:
            if not self._table_has_row(table, iid):
                _log.warning('No data for image %s'%(iid))
                continue
            b.put(iid, {'metadata:is_active': FALSE})
        b.send()
