"""
Function is called when an AJAX request carrying a worker ID hits the webserver. It determines what needs to be shown.
"""
from ..conf import *
from ..database import get as dbget
from ..database import set as dbset
import make_html


def fetch_task(conn, workerId, taskId, is_preview=False):
    """
    Constructs a task after a request hits the webserver. In contrast to build_task, this is for requests that have a
    task ID encoded in them--i.e., the request is for a specific task. It does not check if the worker is banned or if
    they need a practice instead of a normal task. Instead, these data are presumed to be encoded in the MTurk
    structure.

    :param conn: The HappyBase connection object.
    :param workerId: The worker ID, as a string.
    :param taskId: The task ID, as a string.
    :return: The HTML for the requested task.
    """
    # check that the worker exists, else register them. We want to have their information in the database so we don't
    # spawn errors down the road.
    if not dbget.worker_exists(conn, workerId):
        dbset.register_worker(conn, workerId)
    # check if we need demographics or not
    is_practice = dbget.task_is_practice(conn, taskId)
    collect_demo = False
    if dbget.worker_need_demographics(conn, workerId):
        collect_demo = True
    if is_practice:
        dbset.practice_served(conn, taskId, workerId)
    else:
        dbset.task_served(conn, taskId, workerId)
    blocks = dbget.get_task_blocks(conn, taskId)
    if blocks is None:
        # display an error-fetching-task page.
        return make_html.make_error_fetching_task_html(conn, workerId)
    html = make_html.make(blocks, practice=is_practice, collect_demo=collect_demo, is_preview=is_preview)
    # TODO: add the html to the database
    if not is_practice:
        dbset.set_task_html(conn, taskId, html)
    return html


def naive_build_task(conn, workerId, is_preview=False):
    """
    Constructs a task after a request hits the webserver, requiring only the workerId.

    :param conn: The HappyBase connection object.
    :param workerId: The worker ID, as a string.
    :return: The HTML for the request.
    """
    # check that the worker exists, else register them. We want to have their information in the database so we don't
    # spawn errors down the road.
    if not dbget.worker_exists(conn, workerId):
        dbset.register_worker(conn, workerId)
    # check if the worker is banned
    if dbget.worker_is_banned(conn, workerId):
        if dbset.worker_ban_expires_in(conn, workerId):
            # display the ban page
            return make_html.make_ban_html(conn, workerId)
    # check if the worker has completed too many tasks for this week
    if dbget.worker_attempted_too_much(conn, workerId):
        # display the too-many-practice page
        return make_html.make_practice_limit_html(conn, workerId)
    # TODO: check if we've paid out too much this week already?
    # check if we need demographics or not
    # check if we need to give the worker a practice
    is_practice = False
    practice_n = None
    collect_demo = False
    if dbget.worker_need_practice(conn, workerId):
        is_practice = True
        practice_n = dbget.current_worker_practices_number(conn, workerId)
        if dbget.worker_need_demographics(conn, workerId):
            collect_demo = True
    taskId = dbget.get_available_task(conn, practice=is_practice, practice_n=practice_n)
    # indicate that the tasks have been served
    if taskId is None:
        # display no task available page
        # (currently a generic error page)
        return make_html.make_error_fetching_task_html(conn, workerId)
    if is_practice:
        dbset.practice_served(conn, taskId, workerId)
    else:
        dbset.task_served(conn, taskId, workerId)
    blocks = dbget.get_task_blocks(conn, taskId)
    if blocks is None:
        # display an error-fetching-task page.
        return make_html.make_error_fetching_task_html(conn, workerId)
    html = make_html.make(blocks, practice=is_practice, collect_demo=collect_demo, is_preview=is_preview)
    # TODO: add the html to the database
    dbset.set_task_html(conn, taskId, html)
    return html