# -*- coding: utf-8 -*-
# Copyright 2015 - 2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from synapse.api.constants import Membership, EventTypes
from synapse.util.async import concurrently_execute
from synapse.util.logcontext import LoggingContext
from synapse.util.metrics import Measure
from synapse.util.caches.response_cache import ResponseCache
from synapse.push.clientformat import format_push_rules_for_user
from synapse.visibility import filter_events_for_client
from synapse.types import SyncNextBatchToken, SyncPaginationState
from synapse.api.errors import Codes, SynapseError
from synapse.storage.tags import (TAG_CHANGE_NEWLY_TAGGED, TAG_CHANGE_ALL_REMOVED)

from twisted.internet import defer

import collections
import logging
import itertools

logger = logging.getLogger(__name__)


SyncConfig = collections.namedtuple("SyncConfig", [
    "user",
    "filter_collection",
    "is_guest",
    "request_key",
    "pagination_config",
])


class SyncPaginationConfig(collections.namedtuple("SyncPaginationConfig", [
    "order",
    "limit",
    "tags",
])):
    def __init__(self, order, limit, tags):
        if order not in SYNC_PAGINATION_VALID_ORDERS:
            raise SynapseError(400, "Invalid 'order'")
        if tags not in SYNC_PAGINATION_VALID_TAGS_OPTIONS:
            raise SynapseError(400, "Invalid 'tags'")

        try:
            limit = int(limit)
        except:
            raise SynapseError(400, "Invalid 'limit'")

        super(SyncPaginationConfig, self).__init__(order, limit, tags)


SYNC_PAGINATION_TAGS_INCLUDE_ALL = "include_all"
SYNC_PAGINATION_TAGS_IGNORE = "ignore"
SYNC_PAGINATION_VALID_TAGS_OPTIONS = (
    SYNC_PAGINATION_TAGS_INCLUDE_ALL, SYNC_PAGINATION_TAGS_IGNORE,
)

SYNC_PAGINATION_ORDER_TS = "o"
SYNC_PAGINATION_VALID_ORDERS = (SYNC_PAGINATION_ORDER_TS,)


SyncExtras = collections.namedtuple("SyncExtras", [
    "paginate",
    "rooms",
])


class TimelineBatch(collections.namedtuple("TimelineBatch", [
    "prev_batch",
    "events",
    "limited",
])):
    __slots__ = []

    def __nonzero__(self):
        """Make the result appear empty if there are no updates. This is used
        to tell if room needs to be part of the sync result.
        """
        return bool(self.events)


class JoinedSyncResult(collections.namedtuple("JoinedSyncResult", [
    "room_id",           # str
    "timeline",          # TimelineBatch
    "state",             # dict[(str, str), FrozenEvent]
    "ephemeral",
    "account_data",
    "unread_notifications",
    "synced",  # bool
])):
    __slots__ = []

    def __nonzero__(self):
        """Make the result appear empty if there are no updates. This is used
        to tell if room needs to be part of the sync result.
        """
        return bool(
            self.timeline
            or self.state
            or self.ephemeral
            or self.account_data
            # nb the notification count does not, er, count: if there's nothing
            # else in the result, we don't need to send it.
        )


class ArchivedSyncResult(collections.namedtuple("ArchivedSyncResult", [
    "room_id",            # str
    "timeline",           # TimelineBatch
    "state",              # dict[(str, str), FrozenEvent]
    "account_data",
])):
    __slots__ = []

    def __nonzero__(self):
        """Make the result appear empty if there are no updates. This is used
        to tell if room needs to be part of the sync result.
        """
        return bool(
            self.timeline
            or self.state
            or self.account_data
        )


class InvitedSyncResult(collections.namedtuple("InvitedSyncResult", [
    "room_id",   # str
    "invite",    # FrozenEvent: the invite event
])):
    __slots__ = []

    def __nonzero__(self):
        """Invited rooms should always be reported to the client"""
        return True


class ErrorSyncResult(collections.namedtuple("ErrorSyncResult", [
    "room_id",   # str
    "errcode",   # str
    "error",     # str
])):
    __slots__ = []

    def __nonzero__(self):
        """Errors should always be reported to the client"""
        return True


class SyncResult(collections.namedtuple("SyncResult", [
    "next_batch",  # Token for the next sync
    "presence",  # List of presence events for the user.
    "account_data",  # List of account_data events for the user.
    "joined",  # JoinedSyncResult for each joined room.
    "invited",  # InvitedSyncResult for each invited room.
    "archived",  # ArchivedSyncResult for each archived room.
    "errors",  # ErrorSyncResult
    "pagination_info",
])):
    __slots__ = []

    def __nonzero__(self):
        """Make the result appear empty if there are no updates. This is used
        to tell if the notifier needs to wait for more events when polling for
        events.
        """
        return bool(
            self.presence or
            self.joined or
            self.invited or
            self.archived or
            self.account_data
        )


class SyncHandler(object):

    def __init__(self, hs):
        self.store = hs.get_datastore()
        self.notifier = hs.get_notifier()
        self.presence_handler = hs.get_presence_handler()
        self.event_sources = hs.get_event_sources()
        self.clock = hs.get_clock()
        self.response_cache = ResponseCache()

    def wait_for_sync_for_user(self, sync_config, batch_token=None, timeout=0,
                               full_state=False, extras=None):
        """Get the sync for a client if we have new data for it now. Otherwise
        wait for new data to arrive on the server. If the timeout expires, then
        return an empty sync result.
        Returns:
            A Deferred SyncResult.
        """
        result = self.response_cache.get(sync_config.request_key)
        if not result:
            result = self.response_cache.set(
                sync_config.request_key,
                self._wait_for_sync_for_user(
                    sync_config, batch_token, timeout, full_state, extras,
                )
            )
        return result

    @defer.inlineCallbacks
    def _wait_for_sync_for_user(self, sync_config, batch_token, timeout,
                                full_state, extras=None):
        context = LoggingContext.current_context()
        if context:
            if batch_token is None:
                context.tag = "initial_sync"
            elif full_state:
                context.tag = "full_state_sync"
            else:
                context.tag = "incremental_sync"

        if timeout == 0 or batch_token is None or full_state:
            # we are going to return immediately, so don't bother calling
            # notifier.wait_for_events.
            result = yield self.generate_sync_result(
                sync_config, batch_token, full_state=full_state, extras=extras,
            )
            defer.returnValue(result)
        else:
            def current_sync_callback(before_token, after_token):
                return self.generate_sync_result(
                    sync_config, batch_token, full_state=False, extras=extras,
                )

            result = yield self.notifier.wait_for_events(
                sync_config.user.to_string(), timeout, current_sync_callback,
                from_token=batch_token.stream_token,
            )
            defer.returnValue(result)

    @defer.inlineCallbacks
    def push_rules_for_user(self, user):
        user_id = user.to_string()
        rules = yield self.store.get_push_rules_for_user(user_id)
        rules = format_push_rules_for_user(user, rules)
        defer.returnValue(rules)

    @defer.inlineCallbacks
    def ephemeral_by_room(self, sync_config, now_token, since_token=None):
        """Get the ephemeral events for each room the user is in
        Args:
            sync_config (SyncConfig): The flags, filters and user for the sync.
            now_token (StreamToken): Where the server is currently up to.
            since_token (StreamToken): Where the server was when the client
                last synced.
        Returns:
            A tuple of the now StreamToken, updated to reflect the which typing
            events are included, and a dict mapping from room_id to a list of
            typing events for that room.
        """

        with Measure(self.clock, "ephemeral_by_room"):
            typing_key = since_token.typing_key if since_token else "0"

            rooms = yield self.store.get_rooms_for_user(sync_config.user.to_string())
            room_ids = [room.room_id for room in rooms]

            typing_source = self.event_sources.sources["typing"]
            typing, typing_key = yield typing_source.get_new_events(
                user=sync_config.user,
                from_key=typing_key,
                limit=sync_config.filter_collection.ephemeral_limit(),
                room_ids=room_ids,
                is_guest=sync_config.is_guest,
            )
            now_token = now_token.copy_and_replace("typing_key", typing_key)

            ephemeral_by_room = {}

            for event in typing:
                # we want to exclude the room_id from the event, but modifying the
                # result returned by the event source is poor form (it might cache
                # the object)
                room_id = event["room_id"]
                event_copy = {k: v for (k, v) in event.iteritems()
                              if k != "room_id"}
                ephemeral_by_room.setdefault(room_id, []).append(event_copy)

            receipt_key = since_token.receipt_key if since_token else "0"

            receipt_source = self.event_sources.sources["receipt"]
            receipts, receipt_key = yield receipt_source.get_new_events(
                user=sync_config.user,
                from_key=receipt_key,
                limit=sync_config.filter_collection.ephemeral_limit(),
                room_ids=room_ids,
                is_guest=sync_config.is_guest,
            )
            now_token = now_token.copy_and_replace("receipt_key", receipt_key)

            for event in receipts:
                room_id = event["room_id"]
                # exclude room id, as above
                event_copy = {k: v for (k, v) in event.iteritems()
                              if k != "room_id"}
                ephemeral_by_room.setdefault(room_id, []).append(event_copy)

        defer.returnValue((now_token, ephemeral_by_room))

    @defer.inlineCallbacks
    def _load_filtered_recents(self, room_id, sync_config, now_token,
                               since_token=None, recents=None, newly_joined_room=False):
        """
        Returns:
            a Deferred TimelineBatch
        """
        with Measure(self.clock, "load_filtered_recents"):
            timeline_limit = sync_config.filter_collection.timeline_limit()

            if recents is None or newly_joined_room or timeline_limit < len(recents):
                limited = True
            else:
                limited = False

            if recents:
                recents = sync_config.filter_collection.filter_room_timeline(recents)
                recents = yield filter_events_for_client(
                    self.store,
                    sync_config.user.to_string(),
                    recents,
                )
            else:
                recents = []

            if not limited:
                defer.returnValue(TimelineBatch(
                    events=recents,
                    prev_batch=now_token,
                    limited=False
                ))

            filtering_factor = 2
            load_limit = max(timeline_limit * filtering_factor, 10)
            max_repeat = 5  # Only try a few times per room, otherwise
            room_key = now_token.room_key
            end_key = room_key

            since_key = None
            if since_token and not newly_joined_room:
                since_key = since_token.room_key

            while limited and len(recents) < timeline_limit and max_repeat:
                events, end_key = yield self.store.get_room_events_stream_for_room(
                    room_id,
                    limit=load_limit + 1,
                    from_key=since_key,
                    to_key=end_key,
                )
                loaded_recents = sync_config.filter_collection.filter_room_timeline(
                    events
                )
                loaded_recents = yield filter_events_for_client(
                    self.store,
                    sync_config.user.to_string(),
                    loaded_recents,
                )
                loaded_recents.extend(recents)
                recents = loaded_recents

                if len(events) <= load_limit:
                    limited = False
                    break
                max_repeat -= 1

            if len(recents) > timeline_limit:
                limited = True
                recents = recents[-timeline_limit:]
                room_key = recents[0].internal_metadata.before

            prev_batch_token = now_token.copy_and_replace(
                "room_key", room_key
            )

        defer.returnValue(TimelineBatch(
            events=recents,
            prev_batch=prev_batch_token,
            limited=limited or newly_joined_room
        ))

    @defer.inlineCallbacks
    def get_state_after_event(self, event):
        """
        Get the room state after the given event

        Args:
            event(synapse.events.EventBase): event of interest

        Returns:
            A Deferred map from ((type, state_key)->Event)
        """
        state = yield self.store.get_state_for_event(event.event_id)
        if event.is_state():
            state = state.copy()
            state[(event.type, event.state_key)] = event
        defer.returnValue(state)

    @defer.inlineCallbacks
    def get_state_at(self, room_id, stream_position):
        """ Get the room state at a particular stream position

        Args:
            room_id(str): room for which to get state
            stream_position(StreamToken): point at which to get state

        Returns:
            A Deferred map from ((type, state_key)->Event)
        """
        last_events, token = yield self.store.get_recent_events_for_room(
            room_id, end_token=stream_position.room_key, limit=1,
        )

        if last_events:
            last_event = last_events[-1]
            state = yield self.get_state_after_event(last_event)

        else:
            # no events in this room - so presumably no state
            state = {}
        defer.returnValue(state)

    @defer.inlineCallbacks
    def compute_state_delta(self, room_id, batch, sync_config, since_token, now_token,
                            full_state):
        """ Works out the differnce in state between the start of the timeline
        and the previous sync.

        Args:
            room_id(str):
            batch(synapse.handlers.sync.TimelineBatch): The timeline batch for
                the room that will be sent to the user.
            sync_config(synapse.handlers.sync.SyncConfig):
            since_token(str|None): Token of the end of the previous batch. May
                be None.
            now_token(str): Token of the end of the current batch.
            full_state(bool): Whether to force returning the full state.

        Returns:
             A deferred new event dictionary
        """
        # TODO(mjark) Check if the state events were received by the server
        # after the previous sync, since we need to include those state
        # updates even if they occured logically before the previous event.
        # TODO(mjark) Check for new redactions in the state events.

        with Measure(self.clock, "compute_state_delta"):
            if full_state:
                if batch:
                    current_state = yield self.store.get_state_for_event(
                        batch.events[-1].event_id
                    )

                    state = yield self.store.get_state_for_event(
                        batch.events[0].event_id
                    )
                else:
                    current_state = yield self.get_state_at(
                        room_id, stream_position=now_token
                    )

                    state = current_state

                timeline_state = {
                    (event.type, event.state_key): event
                    for event in batch.events if event.is_state()
                }

                state = _calculate_state(
                    timeline_contains=timeline_state,
                    timeline_start=state,
                    previous={},
                    current=current_state,
                )
            elif batch.limited:
                state_at_previous_sync = yield self.get_state_at(
                    room_id, stream_position=since_token
                )

                current_state = yield self.store.get_state_for_event(
                    batch.events[-1].event_id
                )

                state_at_timeline_start = yield self.store.get_state_for_event(
                    batch.events[0].event_id
                )

                timeline_state = {
                    (event.type, event.state_key): event
                    for event in batch.events if event.is_state()
                }

                state = _calculate_state(
                    timeline_contains=timeline_state,
                    timeline_start=state_at_timeline_start,
                    previous=state_at_previous_sync,
                    current=current_state,
                )
            else:
                state = {}

            defer.returnValue({
                (e.type, e.state_key): e
                for e in sync_config.filter_collection.filter_room_state(state.values())
            })

    @defer.inlineCallbacks
    def unread_notifs_for_room_id(self, room_id, sync_config):
        with Measure(self.clock, "unread_notifs_for_room_id"):
            last_unread_event_id = yield self.store.get_last_receipt_event_id_for_user(
                user_id=sync_config.user.to_string(),
                room_id=room_id,
                receipt_type="m.read"
            )

            notifs = []
            if last_unread_event_id:
                notifs = yield self.store.get_unread_event_push_actions_by_room_for_user(
                    room_id, sync_config.user.to_string(), last_unread_event_id
                )
                defer.returnValue(notifs)

            # There is no new information in this period, so your notification
            # count is whatever it was last time.
            defer.returnValue(None)

    @defer.inlineCallbacks
    def generate_sync_result(self, sync_config, batch_token=None, full_state=False,
                             extras=None):
        """Generates a sync result.

        Args:
            sync_config (SyncConfig)
            since_token (StreamToken)
            full_state (bool)

        Returns:
            Deferred(SyncResult)
        """

        # NB: The now_token gets changed by some of the generate_sync_* methods,
        # this is due to some of the underlying streams not supporting the ability
        # to query up to a given point.
        # Always use the `now_token` in `SyncResultBuilder`
        now_token = yield self.event_sources.get_current_token()

        sync_result_builder = SyncResultBuilder(
            sync_config, full_state,
            batch_token=batch_token,
            now_token=now_token,
        )

        account_data_by_room = yield self._generate_sync_entry_for_account_data(
            sync_result_builder
        )

        res = yield self._generate_sync_entry_for_rooms(
            sync_result_builder, account_data_by_room, extras,
        )
        newly_joined_rooms, newly_joined_users = res

        yield self._generate_sync_entry_for_presence(
            sync_result_builder, newly_joined_rooms, newly_joined_users
        )

        defer.returnValue(SyncResult(
            presence=sync_result_builder.presence,
            account_data=sync_result_builder.account_data,
            joined=sync_result_builder.joined,
            invited=sync_result_builder.invited,
            archived=sync_result_builder.archived,
            errors=sync_result_builder.errors,
            next_batch=SyncNextBatchToken(
                stream_token=sync_result_builder.now_token,
                pagination_state=sync_result_builder.pagination_state,
            ),
            pagination_info=sync_result_builder.pagination_info,
        ))

    @defer.inlineCallbacks
    def _generate_sync_entry_for_account_data(self, sync_result_builder):
        """Generates the account data portion of the sync response. Populates
        `sync_result_builder` with the result.

        Args:
            sync_result_builder(SyncResultBuilder)

        Returns:
            Deferred(dict): A dictionary containing the per room account data.
        """
        sync_config = sync_result_builder.sync_config
        user_id = sync_result_builder.sync_config.user.to_string()
        since_token = sync_result_builder.since_token

        if since_token and not sync_result_builder.full_state:
            account_data, account_data_by_room = (
                yield self.store.get_updated_account_data_for_user(
                    user_id,
                    since_token.account_data_key,
                )
            )

            push_rules_changed = yield self.store.have_push_rules_changed_for_user(
                user_id, int(since_token.push_rules_key)
            )

            if push_rules_changed:
                account_data["m.push_rules"] = yield self.push_rules_for_user(
                    sync_config.user
                )
        else:
            account_data, account_data_by_room = (
                yield self.store.get_account_data_for_user(
                    sync_config.user.to_string()
                )
            )

            account_data['m.push_rules'] = yield self.push_rules_for_user(
                sync_config.user
            )

        account_data_for_user = sync_config.filter_collection.filter_account_data([
            {"type": account_data_type, "content": content}
            for account_data_type, content in account_data.items()
        ])

        sync_result_builder.account_data = account_data_for_user

        defer.returnValue(account_data_by_room)

    @defer.inlineCallbacks
    def _generate_sync_entry_for_presence(self, sync_result_builder, newly_joined_rooms,
                                          newly_joined_users):
        """Generates the presence portion of the sync response. Populates the
        `sync_result_builder` with the result.

        Args:
            sync_result_builder(SyncResultBuilder)
            newly_joined_rooms(list): List of rooms that the user has joined
                since the last sync (or empty if an initial sync)
            newly_joined_users(list): List of users that have joined rooms
                since the last sync (or empty if an initial sync)
        """
        now_token = sync_result_builder.now_token
        sync_config = sync_result_builder.sync_config
        user = sync_result_builder.sync_config.user

        presence_source = self.event_sources.sources["presence"]

        since_token = sync_result_builder.since_token
        if since_token and not sync_result_builder.full_state:
            presence_key = since_token.presence_key
            include_offline = True
        else:
            presence_key = None
            include_offline = False

        presence, presence_key = yield presence_source.get_new_events(
            user=user,
            from_key=presence_key,
            is_guest=sync_config.is_guest,
            include_offline=include_offline,
        )
        sync_result_builder.now_token = now_token.copy_and_replace(
            "presence_key", presence_key
        )

        extra_users_ids = set(newly_joined_users)
        for room_id in newly_joined_rooms:
            users = yield self.store.get_users_in_room(room_id)
            extra_users_ids.update(users)
        extra_users_ids.discard(user.to_string())

        states = yield self.presence_handler.get_states(
            extra_users_ids,
            as_event=True,
        )
        presence.extend(states)

        # Deduplicate the presence entries so that there's at most one per user
        presence = {p["content"]["user_id"]: p for p in presence}.values()

        presence = sync_config.filter_collection.filter_presence(
            presence
        )

        sync_result_builder.presence = presence

    @defer.inlineCallbacks
    def _generate_sync_entry_for_rooms(self, sync_result_builder, account_data_by_room,
                                       extras):
        """Generates the rooms portion of the sync response. Populates the
        `sync_result_builder` with the result.

        Args:
            sync_result_builder(SyncResultBuilder)
            account_data_by_room(dict): Dictionary of per room account data

        Returns:
            Deferred(tuple): Returns a 2-tuple of
            `(newly_joined_rooms, newly_joined_users)`
        """
        user_id = sync_result_builder.sync_config.user.to_string()
        sync_config = sync_result_builder.sync_config

        now_token, ephemeral_by_room = yield self.ephemeral_by_room(
            sync_result_builder.sync_config,
            now_token=sync_result_builder.now_token,
            since_token=sync_result_builder.since_token,
        )
        sync_result_builder.now_token = now_token

        ignored_account_data = yield self.store.get_global_account_data_by_type_for_user(
            "m.ignored_user_list", user_id=user_id,
        )

        if ignored_account_data:
            ignored_users = ignored_account_data.get("ignored_users", {}).keys()
        else:
            ignored_users = frozenset()

        if sync_result_builder.since_token:
            res = yield self._get_rooms_changed(sync_result_builder, ignored_users)
            room_entries, invited, newly_joined_rooms = res

            tags_by_room = yield self.store.get_updated_tags(
                user_id,
                sync_result_builder.since_token.account_data_key,
            )
        else:
            res = yield self._get_all_rooms(sync_result_builder, ignored_users)
            room_entries, invited, newly_joined_rooms = res

            tags_by_room = yield self.store.get_tags_for_user(user_id)

        if sync_config.pagination_config:
            pagination_config = sync_config.pagination_config
            old_pagination_value = 0
            include_all_tags = pagination_config.tags == SYNC_PAGINATION_TAGS_INCLUDE_ALL
        elif sync_result_builder.pagination_state:
            pagination_config = SyncPaginationConfig(
                order=sync_result_builder.pagination_state.order,
                limit=sync_result_builder.pagination_state.limit,
                tags=sync_result_builder.pagination_state.tags,
            )
            old_pagination_value = sync_result_builder.pagination_state.value
            include_all_tags = pagination_config.tags == SYNC_PAGINATION_TAGS_INCLUDE_ALL
        else:
            pagination_config = None
            old_pagination_value = 0
            include_all_tags = False

        include_map = extras.get("peek", {}) if extras else {}

        if sync_result_builder.pagination_state:
            missing_state = yield self._get_rooms_that_need_full_state(
                room_ids=[r.room_id for r in room_entries],
                sync_config=sync_config,
                since_token=sync_result_builder.since_token,
                pagination_state=sync_result_builder.pagination_state,
            )

            all_tags = yield self.store.get_tags_for_user(user_id)

            if sync_result_builder.since_token:
                stream_id = sync_result_builder.since_token.account_data_key
                tag_changes = yield self.store.get_room_tags_changed(user_id, stream_id)
            else:
                tag_changes = {}

            if missing_state:
                for r in room_entries:
                    if r.room_id in missing_state:
                        if include_all_tags:
                            change = tag_changes.get(r.room_id)
                            if change == TAG_CHANGE_NEWLY_TAGGED:
                                r.since_token = None
                                r.always_include = True
                                r.full_state = True
                                r.would_require_resync = True
                                r.events = None
                                r.synced = True
                                continue
                            elif change == TAG_CHANGE_ALL_REMOVED:
                                r.always_include = True
                                r.synced = False
                                continue
                            elif r.room_id in all_tags:
                                r.always_include = True
                                continue

                        if r.room_id in include_map:
                            since = include_map[r.room_id].get("since", None)
                            if since:
                                tok = SyncNextBatchToken.from_string(since)
                                r.since_token = tok.stream_token
                            else:
                                r.since_token = None
                                r.always_include = True
                                r.full_state = True
                                r.would_require_resync = True
                                r.events = None
                                r.synced = False
                        else:
                            r.full_state = True
                            r.would_require_resync = True

        elif pagination_config and include_all_tags:
            all_tags = yield self.store.get_tags_for_user(user_id)

            for r in room_entries:
                if r.room_id in all_tags:
                    r.always_include = True

        for room_id in set(include_map.keys()) - {r.room_id for r in room_entries}:
            sync_result_builder.errors.append(ErrorSyncResult(
                room_id=room_id,
                errcode=Codes.CANNOT_PEEK,
                error="Cannot peek into requested room",
            ))

        if pagination_config:
            room_ids = [r.room_id for r in room_entries]
            pagination_limit = pagination_config.limit

            extra_limit = extras.get("paginate", {}).get("limit", 0) if extras else 0

            room_map = yield self._get_room_timestamps_at_token(
                room_ids, sync_result_builder.now_token, sync_config,
                pagination_limit + extra_limit + 1,
            )

            limited = False
            if room_map:
                sorted_list = sorted(
                    room_map.items(),
                    key=lambda item: -item[1]
                )

                cutoff_list = sorted_list[:pagination_limit + extra_limit]

                if cutoff_list[pagination_limit:]:
                    new_room_ids = set(r[0] for r in cutoff_list[pagination_limit:])
                    for r in room_entries:
                        if r.room_id in new_room_ids:
                            r.full_state = True
                            r.always_include = True
                            r.since_token = None
                            r.upto_token = now_token
                            r.events = None

                _, bottom_ts = cutoff_list[-1]
                value = bottom_ts

                limited = any(
                    old_pagination_value < r[1] < value
                    for r in sorted_list[pagination_limit + extra_limit:]
                )

                sync_result_builder.pagination_state = SyncPaginationState(
                    order=pagination_config.order, value=value,
                    limit=pagination_limit + extra_limit,
                    tags=pagination_config.tags,
                )

                to_sync_map = {
                    key: value for key, value in cutoff_list
                }
            else:
                to_sync_map = {}

            sync_result_builder.pagination_info["limited"] = limited

            if len(room_map) == len(room_entries):
                sync_result_builder.pagination_state = None

            room_entries = [
                r for r in room_entries
                if r.room_id in to_sync_map or r.always_include
            ]

        sync_result_builder.full_state |= sync_result_builder.since_token is None

        def handle_room_entries(room_entry):
            return self._generate_room_entry(
                sync_result_builder,
                ignored_users,
                room_entry,
                ephemeral=ephemeral_by_room.get(room_entry.room_id, []),
                tags=tags_by_room.get(room_entry.room_id),
                account_data=account_data_by_room.get(room_entry.room_id, {}),
            )

        yield concurrently_execute(handle_room_entries, room_entries, 10)

        sync_result_builder.invited.extend(invited)

        # Now we want to get any newly joined users
        newly_joined_users = set()
        if sync_result_builder.since_token:
            for joined_sync in sync_result_builder.joined:
                it = itertools.chain(
                    joined_sync.timeline.events, joined_sync.state.values()
                )
                for event in it:
                    if event.type == EventTypes.Member:
                        if event.membership == Membership.JOIN:
                            newly_joined_users.add(event.state_key)

        defer.returnValue((newly_joined_rooms, newly_joined_users))

    @defer.inlineCallbacks
    def _get_rooms_changed(self, sync_result_builder, ignored_users):
        """Gets the the changes that have happened since the last sync.

        Args:
            sync_result_builder(SyncResultBuilder)
            ignored_users(set(str)): Set of users ignored by user.

        Returns:
            Deferred(tuple): Returns a tuple of the form:
            `([RoomSyncResultBuilder], [InvitedSyncResult], newly_joined_rooms)`
        """
        user_id = sync_result_builder.sync_config.user.to_string()
        since_token = sync_result_builder.since_token
        now_token = sync_result_builder.now_token
        sync_config = sync_result_builder.sync_config

        assert since_token

        app_service = yield self.store.get_app_service_by_user_id(user_id)
        if app_service:
            rooms = yield self.store.get_app_service_rooms(app_service)
            joined_room_ids = set(r.room_id for r in rooms)
        else:
            rooms = yield self.store.get_rooms_for_user(user_id)
            joined_room_ids = set(r.room_id for r in rooms)

        # Get a list of membership change events that have happened.
        rooms_changed = yield self.store.get_membership_changes_for_user(
            user_id, since_token.room_key, now_token.room_key
        )

        mem_change_events_by_room_id = {}
        for event in rooms_changed:
            mem_change_events_by_room_id.setdefault(event.room_id, []).append(event)

        newly_joined_rooms = []
        room_entries = []
        invited = []
        for room_id, events in mem_change_events_by_room_id.items():
            non_joins = [e for e in events if e.membership != Membership.JOIN]
            has_join = len(non_joins) != len(events)

            # We want to figure out if we joined the room at some point since
            # the last sync (even if we have since left). This is to make sure
            # we do send down the room, and with full state, where necessary
            if room_id in joined_room_ids or has_join:
                old_state = yield self.get_state_at(room_id, since_token)
                old_mem_ev = old_state.get((EventTypes.Member, user_id), None)
                if not old_mem_ev or old_mem_ev.membership != Membership.JOIN:
                    newly_joined_rooms.append(room_id)

                if room_id in joined_room_ids:
                    continue

            if not non_joins:
                continue

            # Only bother if we're still currently invited
            should_invite = non_joins[-1].membership == Membership.INVITE
            if should_invite:
                if event.sender not in ignored_users:
                    room_sync = InvitedSyncResult(room_id, invite=non_joins[-1])
                    if room_sync:
                        invited.append(room_sync)

            # Always include leave/ban events. Just take the last one.
            # TODO: How do we handle ban -> leave in same batch?
            leave_events = [
                e for e in non_joins
                if e.membership in (Membership.LEAVE, Membership.BAN)
            ]

            if leave_events:
                leave_event = leave_events[-1]
                leave_stream_token = yield self.store.get_stream_token_for_event(
                    leave_event.event_id
                )
                leave_token = since_token.copy_and_replace(
                    "room_key", leave_stream_token
                )

                if since_token and since_token.is_after(leave_token):
                    continue

                room_entries.append(RoomSyncResultBuilder(
                    room_id=room_id,
                    rtype="archived",
                    events=None,
                    newly_joined=room_id in newly_joined_rooms,
                    full_state=False,
                    since_token=since_token,
                    upto_token=leave_token,
                ))

        timeline_limit = sync_config.filter_collection.timeline_limit()

        # Get all events for rooms we're currently joined to.
        room_to_events = yield self.store.get_room_events_stream_for_rooms(
            room_ids=joined_room_ids,
            from_key=since_token.room_key,
            to_key=now_token.room_key,
            limit=timeline_limit + 1,
        )

        # We loop through all room ids, even if there are no new events, in case
        # there are non room events taht we need to notify about.
        for room_id in joined_room_ids:
            room_entry = room_to_events.get(room_id, None)

            if room_entry:
                events, start_key = room_entry

                prev_batch_token = now_token.copy_and_replace("room_key", start_key)

                room_entries.append(RoomSyncResultBuilder(
                    room_id=room_id,
                    rtype="joined",
                    events=events,
                    newly_joined=room_id in newly_joined_rooms,
                    full_state=False,
                    since_token=None if room_id in newly_joined_rooms else since_token,
                    upto_token=prev_batch_token,
                ))
            else:
                room_entries.append(RoomSyncResultBuilder(
                    room_id=room_id,
                    rtype="joined",
                    events=[],
                    newly_joined=room_id in newly_joined_rooms,
                    full_state=False,
                    since_token=since_token,
                    upto_token=since_token,
                ))

        defer.returnValue((room_entries, invited, newly_joined_rooms))

    @defer.inlineCallbacks
    def _get_all_rooms(self, sync_result_builder, ignored_users):
        """Returns entries for all rooms for the user.

        Args:
            sync_result_builder(SyncResultBuilder)
            ignored_users(set(str)): Set of users ignored by user.

        Returns:
            Deferred(tuple): Returns a tuple of the form:
            `([RoomSyncResultBuilder], [InvitedSyncResult], [])`
        """

        user_id = sync_result_builder.sync_config.user.to_string()
        since_token = sync_result_builder.since_token
        now_token = sync_result_builder.now_token
        sync_config = sync_result_builder.sync_config

        membership_list = (
            Membership.INVITE, Membership.JOIN, Membership.LEAVE, Membership.BAN
        )

        room_list = yield self.store.get_rooms_for_user_where_membership_is(
            user_id=user_id,
            membership_list=membership_list
        )

        room_entries = []
        invited = []

        for event in room_list:
            if event.membership == Membership.JOIN:
                room_entries.append(RoomSyncResultBuilder(
                    room_id=event.room_id,
                    rtype="joined",
                    events=None,
                    newly_joined=False,
                    full_state=True,
                    since_token=since_token,
                    upto_token=now_token,
                ))
            elif event.membership == Membership.INVITE:
                if event.sender in ignored_users:
                    continue
                invite = yield self.store.get_event(event.event_id)
                invited.append(InvitedSyncResult(
                    room_id=event.room_id,
                    invite=invite,
                ))
            elif event.membership in (Membership.LEAVE, Membership.BAN):
                # Always send down rooms we were banned or kicked from.
                if not sync_config.filter_collection.include_leave:
                    if event.membership == Membership.LEAVE:
                        if user_id == event.sender:
                            continue

                leave_token = now_token.copy_and_replace(
                    "room_key", "s%d" % (event.stream_ordering,)
                )
                room_entries.append(RoomSyncResultBuilder(
                    room_id=event.room_id,
                    rtype="archived",
                    events=None,
                    newly_joined=False,
                    full_state=True,
                    since_token=since_token,
                    upto_token=leave_token,
                ))

        defer.returnValue((room_entries, invited, []))

    @defer.inlineCallbacks
    def _generate_room_entry(self, sync_result_builder, ignored_users,
                             room_builder, ephemeral, tags, account_data):
        """Populates the `joined` and `archived` section of `sync_result_builder`
        based on the `room_builder`.

        Args:
            sync_result_builder(SyncResultBuilder)
            ignored_users(set(str)): Set of users ignored by user.
            room_builder(RoomSyncResultBuilder)
            ephemeral(list): List of new ephemeral events for room
            tags(list): List of *all* tags for room, or None if there has been
                no change.
            account_data(list): List of new account data for room
            always_include(bool): Always include this room in the sync response,
                even if empty.
        """
        newly_joined = room_builder.newly_joined
        always_include = (
            newly_joined
            or sync_result_builder.full_state
            or room_builder.always_include
        )
        full_state = (
            room_builder.full_state
            or newly_joined
            or sync_result_builder.full_state
            or room_builder.would_require_resync
        )
        events = room_builder.events

        # We want to shortcut out as early as possible.
        if not (always_include or account_data or ephemeral):
            if events == [] and tags is None:
                return

        now_token = sync_result_builder.now_token
        sync_config = sync_result_builder.sync_config

        room_id = room_builder.room_id
        since_token = room_builder.since_token
        upto_token = room_builder.upto_token

        batch = yield self._load_filtered_recents(
            room_id, sync_config,
            now_token=upto_token,
            since_token=since_token,
            recents=events,
            newly_joined_room=newly_joined,
        )

        account_data_events = []
        if tags is not None:
            account_data_events.append({
                "type": "m.tag",
                "content": {"tags": tags},
            })

        for account_data_type, content in account_data.items():
            account_data_events.append({
                "type": account_data_type,
                "content": content,
            })

        account_data = sync_config.filter_collection.filter_room_account_data(
            account_data_events
        )

        ephemeral = sync_config.filter_collection.filter_room_ephemeral(ephemeral)

        if not (always_include or batch or account_data or ephemeral):
            return

        if room_builder.would_require_resync:
            since_token = None
            batch = yield self._load_filtered_recents(
                room_id, sync_config,
                now_token=upto_token,
                since_token=since_token,
                recents=None,
                newly_joined_room=newly_joined,
            )

        state = yield self.compute_state_delta(
            room_id, batch, sync_config, since_token, now_token,
            full_state=full_state
        )

        if room_builder.rtype == "joined":
            unread_notifications = {}
            room_sync = JoinedSyncResult(
                room_id=room_id,
                timeline=batch,
                state=state,
                ephemeral=ephemeral,
                account_data=account_data_events,
                unread_notifications=unread_notifications,
                synced=room_builder.synced,
            )

            if room_sync or always_include:
                notifs = yield self.unread_notifs_for_room_id(
                    room_id, sync_config
                )

                if notifs is not None:
                    unread_notifications["notification_count"] = notifs["notify_count"]
                    unread_notifications["highlight_count"] = notifs["highlight_count"]

                sync_result_builder.joined.append(room_sync)
        elif room_builder.rtype == "archived":
            room_sync = ArchivedSyncResult(
                room_id=room_id,
                timeline=batch,
                state=state,
                account_data=account_data,
            )
            if room_sync or always_include:
                sync_result_builder.archived.append(room_sync)
        else:
            raise Exception("Unrecognized rtype: %r", room_builder.rtype)

    @defer.inlineCallbacks
    def _get_room_timestamps_at_token(self, room_ids, token, sync_config, limit):
        room_to_entries = {}

        @defer.inlineCallbacks
        def _get_last_ts(room_id):
            entry = yield self.store.get_last_event_id_ts_for_room(
                room_id, token.room_key
            )

            # TODO: Is this ever possible?
            room_to_entries[room_id] = entry if entry else {
                "origin_server_ts": 0,
            }

        yield concurrently_execute(_get_last_ts, room_ids, 10)

        if len(room_to_entries) <= limit:
            defer.returnValue({
                room_id: entry["origin_server_ts"]
                for room_id, entry in room_to_entries.items()
            })

        queued_events = sorted(
            room_to_entries.items(),
            key=lambda e: -e[1]["origin_server_ts"]
        )

        to_return = {}

        while len(to_return) < limit and len(queued_events) > 0:
            to_fetch = queued_events[:limit - len(to_return)]
            event_to_q = {
                e["event_id"]: (room_id, e) for room_id, e in to_fetch
                if "event_id" in e
            }

            # Now we fetch each event to check if its been filtered out
            event_map = yield self.store.get_events(event_to_q.keys())

            recents = sync_config.filter_collection.filter_room_timeline(
                event_map.values()
            )
            recents = yield filter_events_for_client(
                self.store,
                sync_config.user.to_string(),
                recents,
            )

            to_return.update({r.room_id: r.origin_server_ts for r in recents})

            for ev_id in set(event_map.keys()) - set(r.event_id for r in recents):
                queued_events.append(event_to_q[ev_id])

            # FIXME: Need to refetch TS
            queued_events.sort(key=lambda e: -e[1]["origin_server_ts"])

        defer.returnValue(to_return)

    @defer.inlineCallbacks
    def _get_rooms_that_need_full_state(self, room_ids, sync_config, since_token,
                                        pagination_state):
        start_ts = yield self._get_room_timestamps_at_token(
            room_ids, since_token,
            sync_config=sync_config,
            limit=len(room_ids),
        )

        missing_list = frozenset(
            room_id for room_id, ts in
            sorted(start_ts.items(), key=lambda item: -item[1])
            if ts < pagination_state.value
        )

        defer.returnValue(missing_list)


def _action_has_highlight(actions):
    for action in actions:
        try:
            if action.get("set_tweak", None) == "highlight":
                return action.get("value", True)
        except AttributeError:
            pass

    return False


def _calculate_state(timeline_contains, timeline_start, previous, current):
    """Works out what state to include in a sync response.

    Args:
        timeline_contains (dict): state in the timeline
        timeline_start (dict): state at the start of the timeline
        previous (dict): state at the end of the previous sync (or empty dict
            if this is an initial sync)
        current (dict): state at the end of the timeline

    Returns:
        dict
    """
    event_id_to_state = {
        e.event_id: e
        for e in itertools.chain(
            timeline_contains.values(),
            previous.values(),
            timeline_start.values(),
            current.values(),
        )
    }

    c_ids = set(e.event_id for e in current.values())
    tc_ids = set(e.event_id for e in timeline_contains.values())
    p_ids = set(e.event_id for e in previous.values())
    ts_ids = set(e.event_id for e in timeline_start.values())

    state_ids = ((c_ids | ts_ids) - p_ids) - tc_ids

    evs = (event_id_to_state[e] for e in state_ids)
    return {
        (e.type, e.state_key): e
        for e in evs
    }


class SyncResultBuilder(object):
    "Used to help build up a new SyncResult for a user"

    __slots__ = (
        "sync_config", "full_state", "batch_token", "since_token", "pagination_state",
        "now_token", "presence", "account_data", "joined", "invited", "archived",
        "pagination_info", "errors",
    )

    def __init__(self, sync_config, full_state, batch_token, now_token):
        """
        Args:
            sync_config(SyncConfig)
            full_state(bool): The full_state flag as specified by user
            batch_token(SyncNextBatchToken): The token supplied by user, or None.
            now_token(StreamToken): The token to sync up to.
        """
        self.sync_config = sync_config
        self.full_state = full_state
        self.batch_token = batch_token
        self.since_token = batch_token.stream_token if batch_token else None
        self.pagination_state = batch_token.pagination_state if batch_token else None
        self.now_token = now_token

        self.presence = []
        self.account_data = []
        self.joined = []
        self.invited = []
        self.archived = []
        self.errors = []

        self.pagination_info = {}


class RoomSyncResultBuilder(object):
    """Stores information needed to create either a `JoinedSyncResult` or
    `ArchivedSyncResult`.
    """

    __slots__ = (
        "room_id", "rtype", "events", "newly_joined", "full_state", "since_token",
        "upto_token", "always_include", "would_require_resync", "synced",
    )

    def __init__(self, room_id, rtype, events, newly_joined, full_state,
                 since_token, upto_token):
        """
        Args:
            room_id(str)
            rtype(str): One of `"joined"` or `"archived"`
            events(list): List of events to include in the room, (more events
                may be added when generating result).
            newly_joined(bool): If the user has newly joined the room
            full_state(bool): Whether the full state should be sent in result
            since_token(StreamToken): Earliest point to return events from, or None
            upto_token(StreamToken): Latest point to return events from.
        """
        self.room_id = room_id
        self.rtype = rtype
        self.events = events
        self.newly_joined = newly_joined
        self.full_state = full_state
        self.since_token = since_token
        self.upto_token = upto_token
        self.always_include = False
        self.would_require_resync = False
        self.synced = True
