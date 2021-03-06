from __future__ import print_function, unicode_literals

import logging
import six
import time


from celery.task import task
from collections import defaultdict
from datetime import timedelta
from django.core.cache import cache
from django.db import transaction, connection
from django.db.models import Count
from django.utils import timezone
from django_redis import get_redis_connection
from temba.utils.mage import mage_handle_new_message, mage_handle_new_contact
from temba.utils.queues import start_task, complete_task, nonoverlapping_task
from temba.utils import json_date_to_datetime, chunk_list
from .models import Msg, Broadcast, BroadcastRecipient, ExportMessagesTask, PENDING, HANDLE_EVENT_TASK, MSG_EVENT
from .models import FIRE_EVENT, TIMEOUT_EVENT, SystemLabel

logger = logging.getLogger(__name__)


def process_run_timeout(run_id, timeout_on):
    """
    Processes a single run timeout
    """
    from temba.flows.models import FlowRun

    r = get_redis_connection()
    run = FlowRun.objects.filter(id=run_id, is_active=True, flow__is_active=True).first()

    if run:
        key = 'pcm_%d' % run.contact_id
        if not r.get(key):
            with r.lock(key, timeout=120):
                print("T[%09d] Processing timeout" % run.id)
                start = time.time()

                run.refresh_from_db()

                # this is still the timeout to process (json doesn't have microseconds so close enough)
                if run.timeout_on and abs(run.timeout_on - timeout_on) < timedelta(milliseconds=1):
                    run.resume_after_timeout(timeout_on)
                else:
                    print("T[%09d] .. skipping timeout, already handled" % run.id)

                print("T[%09d] %08.3f s" % (run.id, time.time() - start))


@task(track_started=True, name='process_message_task')  # pragma: no cover
def process_message_task(msg_id, from_mage=False, new_contact=False):
    """
    Processes a single incoming message through our queue.
    """
    r = get_redis_connection()
    msg = Msg.objects.filter(pk=msg_id, status=PENDING).select_related('org', 'contact', 'contact_urn', 'channel').first()

    # somebody already handled this message, move on
    if not msg:  # pragma: needs cover
        return

    # get a lock on this contact, we process messages one by one to prevent odd behavior in flow processing
    key = 'pcm_%d' % msg.contact_id
    if not r.get(key):
        with r.lock(key, timeout=120):
            print("M[%09d] Processing - %s" % (msg.id, msg.text))
            start = time.time()

            # if message was created in Mage...
            if from_mage:
                mage_handle_new_message(msg.org, msg)
                if new_contact:
                    mage_handle_new_contact(msg.org, msg.contact)

            Msg.process_message(msg)
            print("M[%09d] %08.3f s - %s" % (msg.id, time.time() - start, msg.text))


@task(track_started=True, name='send_broadcast')
def send_broadcast_task(broadcast_id):
    # get our broadcast
    from .models import Broadcast
    broadcast = Broadcast.objects.get(pk=broadcast_id)
    broadcast.send()


@task(track_started=True, name='send_spam')
def send_spam(user_id, contact_id):  # pragma: no cover
    """
    Processses a single incoming message through our queue.
    """
    from django.contrib.auth.models import User
    from temba.contacts.models import Contact, TEL_SCHEME
    from temba.msgs.models import Broadcast

    contact = Contact.all().get(pk=contact_id)
    user = User.objects.get(pk=user_id)
    channel = contact.org.get_send_channel(TEL_SCHEME)

    if not channel:  # pragma: no cover
        print("Sorry, no channel to be all spammy with")
        return

    long_text = "Test Message #%d. The path of the righteous man is beset on all sides by the iniquities of the " \
                "selfish and the tyranny of evil men. Blessed is your face."

    # only trigger sync on the last one
    for idx in range(10):
        broadcast = Broadcast.create(contact.org, user, long_text % (idx + 1), [contact])
        broadcast.send(trigger_send=(idx == 149))


@task(track_started=True, name='fail_old_messages')
def fail_old_messages():  # pragma: needs cover
    Msg.fail_old_messages()


@nonoverlapping_task(track_started=True, name='collect_message_metrics_task', time_limit=900)
def collect_message_metrics_task():  # pragma: needs cover
    """
    Collects message metrics and sends them to our analytics.
    """
    from .models import INCOMING, OUTGOING, PENDING, QUEUED, ERRORED, INITIALIZING
    from temba.utils import analytics

    # current # of queued messages (excluding Android)
    count = Msg.objects.filter(direction=OUTGOING, status=QUEUED).exclude(channel=None).\
        exclude(topup=None).exclude(channel__channel_type='A').exclude(next_attempt__gte=timezone.now()).count()
    analytics.gauge('temba.current_outgoing_queued', count)

    # current # of initializing messages (excluding Android)
    count = Msg.objects.filter(direction=OUTGOING, status=INITIALIZING).exclude(channel=None).exclude(topup=None).exclude(channel__channel_type='A').count()
    analytics.gauge('temba.current_outgoing_initializing', count)

    # current # of pending messages (excluding Android)
    count = Msg.objects.filter(direction=OUTGOING, status=PENDING).exclude(channel=None).exclude(topup=None).exclude(channel__channel_type='A').count()
    analytics.gauge('temba.current_outgoing_pending', count)

    # current # of errored messages (excluding Android)
    count = Msg.objects.filter(direction=OUTGOING, status=ERRORED).exclude(channel=None).exclude(topup=None).exclude(channel__channel_type='A').count()
    analytics.gauge('temba.current_outgoing_errored', count)

    # current # of android outgoing messages waiting to be sent
    count = Msg.objects.filter(direction=OUTGOING, status__in=[PENDING, QUEUED], channel__channel_type='A').exclude(channel=None).exclude(topup=None).count()
    analytics.gauge('temba.current_outgoing_android', count)

    # current # of pending incoming messages that haven't yet been handled
    count = Msg.objects.filter(direction=INCOMING, status=PENDING).exclude(channel=None).count()
    analytics.gauge('temba.current_incoming_pending', count)

    # stuff into redis when we last run, we do this as a canary as to whether our tasks are falling behind or not running
    cache.set('last_cron', timezone.now())


@nonoverlapping_task(track_started=True, name='check_messages_task', time_limit=900)
def check_messages_task():  # pragma: needs cover
    """
    Checks to see if any of our aggregators have errored messages that need to be retried.
    Also takes care of flipping Contacts from Failed to Normal and back based on their status.
    """
    from django.utils import timezone
    from .models import INCOMING, PENDING
    from temba.orgs.models import Org
    from temba.channels.tasks import send_msg_task
    from temba.flows.tasks import start_msg_flow_batch_task

    now = timezone.now()
    five_minutes_ago = now - timedelta(minutes=5)
    r = get_redis_connection()

    # for any org that sent messages in the past five minutes, check for pending messages
    for org in Org.objects.filter(msgs__created_on__gte=five_minutes_ago).distinct():
        # more than 1,000 messages queued? don't do anything, wait for our queue to go down
        queued = r.zcard('send_message_task:%d' % org.id)
        if queued < 1000:
            org.trigger_send()

    # fire a few send msg tasks in case we dropped one somewhere during a restart
    # (these will be no-ops if there is nothing to do)
    for i in range(100):
        send_msg_task.apply_async(queue='msgs')
        handle_event_task.apply_async(queue='handler')
        start_msg_flow_batch_task.apply_async(queue='flows')

    # also check any incoming messages that are still pending somehow, reschedule them to be handled
    unhandled_messages = Msg.objects.filter(direction=INCOMING, status=PENDING, created_on__lte=five_minutes_ago)
    unhandled_messages = unhandled_messages.exclude(channel__org=None).exclude(contact__is_test=True)
    unhandled_count = unhandled_messages.count()

    if unhandled_count:
        print("** Found %d unhandled messages" % unhandled_count)
        for msg in unhandled_messages[:100]:
            msg.handle()


@task(track_started=True, name='export_sms_task')
def export_sms_task(id):  # pragma: needs cover
    """
    Export messages to a file and e-mail a link to the user
    """
    export_task = ExportMessagesTask.objects.filter(pk=id).first()
    if export_task:
        export_task.start_export()


@task(track_started=True, name="handle_event_task", time_limit=180, soft_time_limit=120)
def handle_event_task():
    """
    Priority queue task that handles both event fires (when fired) and new incoming
    messages that need to be handled.

    Currently three types of events may be "popped" from our queue:
           msg - Which contains the id of the Msg to be processed
          fire - Which contains the id of the EventFire that needs to be fired
       timeout - Which contains a run that timed out and needs to be resumed
    """
    from temba.campaigns.models import EventFire
    r = get_redis_connection()

    # pop off the next task
    org_id, event_task = start_task(HANDLE_EVENT_TASK)

    # it is possible we have no message to send, if so, just return
    if not event_task:  # pragma: needs cover
        return

    try:
        if event_task['type'] == MSG_EVENT:
            process_message_task(event_task['id'], event_task.get('from_mage', False), event_task.get('new_contact', False))

        elif event_task['type'] == FIRE_EVENT:
            # use a lock to make sure we don't do two at once somehow
            key = 'fire_campaign_%s' % event_task['id']
            if not r.get(key):
                with r.lock(key, timeout=120):
                    event = EventFire.objects.filter(pk=event_task['id'], fired=None)\
                                             .select_related('event', 'event__campaign', 'event__campaign__org').first()
                    if event:
                        print("E[%09d] Firing for org: %s" % (event.id, event.event.campaign.org.name))
                        start = time.time()
                        event.fire()
                        print("E[%09d] %08.3f s" % (event.id, time.time() - start))

        elif event_task['type'] == TIMEOUT_EVENT:
            timeout_on = json_date_to_datetime(event_task['timeout_on'])
            process_run_timeout(event_task['run'], timeout_on)

        else:  # pragma: needs cover
            raise Exception("Unexpected event type: %s" % event_task)
    finally:
        complete_task(HANDLE_EVENT_TASK, org_id)


@nonoverlapping_task(track_started=True, name='purge_broadcasts_task', time_limit=60 * 60 * 24 * 7)
def purge_broadcasts_task():
    """
    Looks for broadcasts older than 90 days and marks their messages as purged
    """
    from temba.orgs.models import Debit, Org

    purge_before = timezone.now() - timedelta(days=90)  # 90 days ago

    print("[PURGE] Starting purge broadcasts task...")

    # determine which orgs are purgeable
    purgeable_orgs = list(Org.objects.filter(is_purgeable=True))

    # determine which broadcasts are old
    purge_ids = list(Broadcast.objects.filter(org__in=purgeable_orgs, created_on__lt=purge_before,
                                              purged=False).values_list('pk', flat=True))
    bcasts_purged = 0
    msgs_deleted = 0

    print("[PURGE] Found %d broadcasts created before %s..." % (len(purge_ids), purge_before))

    for batch_ids in chunk_list(purge_ids, 1000):
        batch_broadcasts = Broadcast.objects.filter(pk__in=batch_ids)
        batch_message_ids = []  # all the message ids in these broadcasts
        batch_contact_ids_by_status = defaultdict(list)
        batch_topup_counts = defaultdict(int)  # message counts per topup in these broadcasts

        with transaction.atomic():

            # get the topup message counts and message ids for these broadcasts
            for broadcast in batch_broadcasts:
                topup_counts = broadcast.msgs.values('topup_id').annotate(count=Count('topup_id'))
                for tc in topup_counts:
                    if tc['topup_id']:
                        batch_topup_counts[tc['topup_id']] += tc['count']

                for msg_id, msg_bcast, msg_status, contact_id in list(broadcast.msgs.values_list('id', 'broadcast', 'status', 'contact')):
                    batch_message_ids.append(msg_id)
                    batch_contact_ids_by_status[(msg_bcast, msg_status)].append(contact_id)

            print("[PURGE] Gathered topup counts and message list (%d topups, %d messages)" % (len(batch_topup_counts), len(batch_message_ids)))

            # create debit objects for each topup
            for topup_id, msg_count in six.iteritems(batch_topup_counts):
                Debit.objects.create(topup_id=topup_id, amount=msg_count, debit_type=Debit.TYPE_PURGE)

            print("[PURGE] Created debits for each topup (%d debits)" % len(batch_topup_counts))

            # update the broadcast recipient records with the statuses of the messages we're about to delete
            non_sent_recipients = 0
            for (msg_bcast, msg_status), contact_ids in six.iteritems(batch_contact_ids_by_status):
                for contact_ids_batch in chunk_list(contact_ids, 1000):
                    recipients = BroadcastRecipient.objects.filter(broadcast=msg_bcast, contact_id__in=contact_ids_batch)
                    recipients.update(purged_status=msg_status)

                non_sent_recipients += len(contact_ids)

            print("[PURGE] Updated broadcast recipients with non-sent status (%d recipients)" % non_sent_recipients)

            # delete messages in batches to avoid long locks
            for msg_ids_batch in chunk_list(batch_message_ids, 1000):
                # manually delete to avoid slow and unnecessary checks on related fields like response_to
                cursor = connection.cursor()

                msg_ids = tuple(msg_ids_batch)

                cursor.execute('DELETE FROM channels_channellog WHERE msg_id IN %s', params=[msg_ids])
                cursor.execute('DELETE FROM flows_flowstep_messages WHERE msg_id IN %s', params=[msg_ids])
                cursor.execute('DELETE FROM msgs_msg WHERE id IN %s', params=[msg_ids])

            print("[PURGE] Deleted messages (%d messages)" % len(batch_message_ids))

            # mark these broadcasts as purged
            batch_broadcasts.update(purged=True)

            print("[PURGE] Updated broadcasts as purged (%d broadcasts)" % len(batch_ids))

        bcasts_purged += len(batch_ids)
        msgs_deleted += len(batch_message_ids)

        print("[PURGE] Purged %d of %d broadcasts (%d messages deleted)" % (bcasts_purged, len(purge_ids), msgs_deleted))

    Debit.squash()

    print("[PURGE] Finished purging %d broadcasts older than %s, deleting %d messages" % (len(purge_ids), purge_before, msgs_deleted))


@nonoverlapping_task(track_started=True, name="squash_systemlabels")
def squash_systemlabels():
    SystemLabel.squash()


@nonoverlapping_task(track_started=True, name='clear_old_msg_external_ids', time_limit=60 * 60 * 36)
def clear_old_msg_external_ids():
    """
    Clears external_id on older messages to reduce the size of the index on that column. External ids aren't surfaced
    anywhere and are only used for debugging channel issues, so are of limited usefulness on older messages.
    """
    threshold = timezone.now() - timedelta(days=30)  # 30 days ago

    msg_ids = list(Msg.objects.filter(created_on__lt=threshold).exclude(external_id=None).values_list('id', flat=True))

    for msg_id_batch in chunk_list(msg_ids, 1000):
        Msg.objects.filter(pk__in=msg_id_batch).update(external_id=None)

    print("Cleared external ids on %d messages" % len(msg_ids))
