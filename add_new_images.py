"""
This is a utility script.

Exports a function that adds all the newly discovered images.

NOTE:
    This will have to be updated if we add more images from a different
    location.
"""

import boto3
from conf import *

def _parse_key(key):
    vals = key.split('.')[0].split('_')
    sim = float(vals[-1]) / 10000
    ttype = vals[-2]
    vid = '_'.join(vals[:-2])
    return vid, sim, ttype


def _yt_filt_func(key):
    _, sim, _ = _parse_key(key)
    return sim < 0.027


def _s3sourcer(bucket_name, filter_func=None):
    """
    Constructs an iterator over the items in a bucket, returning those that
    pass the filter function. The iterator returns image id, url tuples.

    :param bucket_name: The name of the bucket to iterate over.
    :param filter_func: The filter function, which checks if an item should be
    yielded based on its key.
    :return: None.
    """
    if filter_func is None:
        filter_func = lambda x: True
    AWS_ACCESS_ID = 'AKIAIS3LLKRK7HDX4XYA'
    AWS_SECRET_KEY = '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'
    s3 = boto3.resource('s3', aws_access_key_id=AWS_ACCESS_ID,
                        aws_secret_access_key=AWS_SECRET_KEY)
    bucket_iter = iter(s3.Bucket(bucket_name).objects.all())
    base_url = 'https://s3.amazonaws.com/%s/%s'
    while True:
        item = bucket_iter.next()
        if filter_func(item.key):
            image_id = item.key.split('.')[0]
            image_url = base_url % (bucket_name, item.key)
            yield (image_id, image_url)
    return


def update(dbset, dbget, dry_run=False):
    sources = [_s3sourcer('neon-image-library'),
               _s3sourcer('mturk-youtube-thumbs',
                          filter_func=_yt_filt_func)]
    to_add_ids = []
    to_add_urls = []
    with dbget.pool.connection() as conn:
        # just use dbget's connection, fuck it.
        table = conn.table(IMAGE_TABLE)
        for n, source in enumerate(sources):
            for m, (imid, imurl) in enumerate(source):
                if not m % 1000:
                    print '%i - %i' % (n, m)
                if not dbget.table_has_key(table, imid):
                    to_add_ids.append(imid)
                    to_add_urls.append(imurl)
    if not dry_run:
        dbset.register_images(to_add_ids, to_add_urls)