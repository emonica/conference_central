#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'


from datetime import datetime, time
from time import strftime

import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb
from google.net.proto.ProtocolBuffer import ProtocolBufferDecodeError

from models import ConflictException
from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import StringMessage
from models import BooleanMessage
from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import ConferenceQueryForm
from models import ConferenceQueryForms
from models import TeeShirtSize
from models import Session
from models import SessionForm
from models import SessionForms
from models import Speaker
from models import SpeakerForm
from models import SpeakerForms

from settings import WEB_CLIENT_ID
from settings import ANDROID_CLIENT_ID
from settings import IOS_CLIENT_ID
from settings import ANDROID_AUDIENCE

from utils import getUserId

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"
ANNOUNCEMENT_TPL = ('Last chance to attend! The following conferences '
                    'are nearly sold out: %s')
# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": [ "Default", "Topic" ],
}

OPERATORS = {
            'EQ':   '=',
            'GT':   '>',
            'GTEQ': '>=',
            'LT':   '<',
            'LTEQ': '<=',
            'NE':   '!='
            }

FIELDS =    {
            'CITY': 'city',
            'TOPIC': 'topics',
            'MONTH': 'month',
            'MAX_ATTENDEES': 'maxAttendees',
            }

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

CONF_TYPE_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
    typeOfSession=messages.StringField(2),
)

CONF_POST_REQUEST = endpoints.ResourceContainer(
    ConferenceForm,
    websafeConferenceKey=messages.StringField(2),
)

SESSION_POST_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    websafeConferenceKey=messages.StringField(2),
)

SPEAKER_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    speakerId=messages.StringField(1),
)

WISHLIST_POST_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    sessionKey=messages.StringField(1),
)

CONF_IN_TOWN_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    city=messages.StringField(1),
    startDate=messages.StringField(2),
    endDate=messages.StringField(3),
)

CONF_POPULAR_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    topic=messages.StringField(1),
)

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -


@endpoints.api(name='conference', version='v1', audiences=[ANDROID_AUDIENCE],
    allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID, ANDROID_CLIENT_ID, IOS_CLIENT_ID],
    scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

# - - - Conference objects - - - - - - - - - - - - - - - - -

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf


    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
        # generate Profile Key based on user ID and Conference
        # ID based on Profile key get Conference key from ID
        p_key = ndb.Key(Profile, user_id)
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id

        # create Conference, send email to organizer confirming
        # creation of Conference & return (modified) ConferenceForm
        Conference(**data).put()
        taskqueue.add(params={'email': user.email(),
            'conferenceInfo': repr(request)},
            url='/tasks/send_confirmation_email'
        )
        return request


    @ndb.transactional()
    def _updateConferenceObject(self, request):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}

        # check that conference exists and update it
        try:
            conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        except ProtocolBufferDecodeError: 
            print('No conference found with key: %s' % request.websafeConferenceKey)
            raise

        # check that user is owner
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can update the conference.')

        # Not getting all the fields, so don't create a new object; just
        # copy relevant fields from ConferenceForm to Conference object
        for field in request.all_fields():
            data = getattr(request, field.name)
            # only copy fields where we get data
            if data not in (None, []):
                # special handling for dates (convert string to Date)
                if field.name in ('startDate', 'endDate'):
                    data = datetime.strptime(data, "%Y-%m-%d").date()
                    if field.name == 'startDate':
                        conf.month = data.month
                # write to Conference object
                setattr(conf, field.name, data)
        conf.put()
        prof = ndb.Key(Profile, user_id).get()
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(ConferenceForm, ConferenceForm, path='conference',
            http_method='POST', name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)


    @endpoints.method(CONF_POST_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='PUT', name='updateConference')
    def updateConference(self, request):
        """Update conference w/provided fields & return w/updated info."""
        return self._updateConferenceObject(request)


    @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='GET', name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        try:
            conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        except ProtocolBufferDecodeError:
            print('No conference found with key: %s' % request.websafeConferenceKey)
            raise

        prof = conf.key.parent().get()
        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='getConferencesCreated',
            http_method='POST', name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # create ancestor query for all key matches for this user
        confs = Conference.query(ancestor=ndb.Key(Profile, user_id))
        prof = ndb.Key(Profile, user_id).get()
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, getattr(prof, 'displayName')) for conf in confs]
        )


    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q


    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException("Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)


    @endpoints.method(ConferenceQueryForms, ConferenceForms,
            path='queryConferences',
            http_method='POST',
            name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)

        # need to fetch organiser displayName from profiles
        # get all keys and use get_multi for speed
        organisers = [(ndb.Key(Profile, conf.organizerUserId)) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return individual ConferenceForm object per Conference
        return ConferenceForms(
                items=[self._copyConferenceToForm(conf, names[conf.organizerUserId]) for conf in \
                conferences]
        )


# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
                elif field.name == 'sessionWishlist':
                    setattr(pf, field.name, [k.urlsafe() for k in getattr(prof, field.name)])
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf


    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # get Profile from datastore
        user_id = getUserId(user)
        p_key = ndb.Key(Profile, user_id)
        profile = p_key.get()
        # create new Profile if not there
        if not profile:
            profile = Profile(
                key = p_key,
                displayName = user.nickname(),
                mainEmail= user.email(),
                teeShirtSize = str(TeeShirtSize.NOT_SPECIFIED),
                sessionWishlist = [],
            )
            profile.put()

        return profile      # return Profile


    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
                        prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)


    @endpoints.method(message_types.VoidMessage, ProfileForm,
            path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()


    @endpoints.method(ProfileMiniForm, ProfileForm,
            path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)


# - - - Announcements - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = ANNOUNCEMENT_TPL % (
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

        return announcement


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='conference/announcement/get',
            http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        return StringMessage(data=memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY) or "")


# - - - Registration - - - - - - - - - - - - - - - - - - - -

    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        
        try:
            conf = ndb.Key(urlsafe=wsck).get()
        except ProtocolBufferDecodeError:
            print('No conference found with key: %s' % wsck)
            raise

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='conferences/attending',
            http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        prof = self._getProfileFromUser() # get user Profile
        conf_keys = [ndb.Key(urlsafe=wsck) for wsck in prof.conferenceKeysToAttend]
        conferences = ndb.get_multi(conf_keys)

        # get organizers
        organisers = [ndb.Key(Profile, conf.organizerUserId) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(items=[self._copyConferenceToForm(conf, names[conf.organizerUserId])\
         for conf in conferences]
        )


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='DELETE', name='unregisterFromConference')
    def unregisterFromConference(self, request):
        """Unregister user for selected conference."""
        return self._conferenceRegistration(request, reg=False)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='filterPlayground',
            http_method='GET', name='filterPlayground')
    def filterPlayground(self, request):
        """Filter Playground"""
        q = Conference.query()
        q = q.filter(Conference.city == "London")
        q = q.filter(Conference.topics == "Medical Innovations")
        q = q.filter(Conference.month == 6)

        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in q]
        )

# - - - Speakers - - - - - - - - - - - - - - - - - - - - - - - - -
    def _copySpeakerToForm(self, speaker):
        """Copy relevant fields from Speaker to SpeakerForm."""
        sf = SpeakerForm()
        for field in sf.all_fields():
            if hasattr(speaker, field.name):
                setattr(sf, field.name, getattr(speaker, field.name))
        sf.check_initialized()
        return sf

    def _getSpeaker(self, name, email):
        """Return Speaker from datastore, creating new one if non-existent."""
        # get Speaker from datastore
        s_key = ndb.Key(Speaker, email)
        speaker = s_key.get()
        # create new Speaker if not there
        if not speaker:
            speaker = Speaker(
                key = s_key,
                name = name,
                email= email)
            speaker.put()
        else:
            #check name is the same, else return error 
            if speaker.name != name:
                raise ConflictException(
                    "A different speaker is already registered with this email %s" % email)

        return speaker      # return Speaker

    @endpoints.method(message_types.VoidMessage, SpeakerForms,
            path='getSpeakers',
            http_method='GET',
            name='getSpeakers')
    def getSpeakers(self, request):
        """Query for speakers."""
        speakers = Speaker.query()

        return SpeakerForms(
                items=[self._copySpeakerToForm(speaker) for speaker in speakers]
        )


    @endpoints.method(SPEAKER_GET_REQUEST, SpeakerForm,
            path='speaker/detail/{speakerId}',
            http_method='GET',
            name='getSpeakerDetails')
    def getSpeakerDetails(self, request):
        """Get details of a certain speaker"""
        s_key = ndb.Key(Speaker, request.speakerId)
        speaker = s_key.get()

        return self._copySpeakerToForm(speaker)

# - - - Conference sessions - - - - - - - - - - - - - - - - - - - -

    def _copySessionToForm(self, session):
        """Copy relevant fields from Session to SessionForm."""
        sf = SessionForm()
        for field in sf.all_fields():
            if hasattr(session, field.name):
                # convert Date to date string; just copy others
                if field.name == "date":
                    setattr(sf, field.name, str(getattr(session, field.name)))
                elif field.name == "startTime":
                    stime = getattr(session, field.name)
                    if stime:
                        setattr(sf, field.name, stime.strftime('%H:%M'))
                    else:
                        setattr(sf, field.name, 'N/A')
                else:
                    setattr(sf, field.name, getattr(session, field.name))
            elif field.name == "websafeKey":
                setattr(sf, field.name, session.key.urlsafe())
        sf.check_initialized()
        return sf

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf

    def _createSessionObject(self, request):
        """Create conference Session object, returning SessionForm/request"""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # check that conference exists
        # and get conference details 
        try:
            c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
            conf = c_key.get()
        except ProtocolBufferDecodeError:
            print('No conference found with key: %s' % request.websafeConferenceKey)
            raise
        
        # check that user is owner
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the conference owner can add sessions.')

        if not request.name:
            raise endpoints.BadRequestException("Session 'name' field required")

        # copy SessionForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeConferenceKey']

        # convert date and startTime from string to Date/Time objects; 
        if data['date']:
            data['date'] = datetime.strptime(data['date'][:10], "%Y-%m-%d").date()
        if data['startTime']:
            data['startTime'] = datetime.strptime(data['startTime'][:5], "%H:%M").time()

        # Add speaker id
        if data['speakerName'] and data['speakerEmail']:
            speaker = self._getSpeaker(data['speakerName'], data['speakerEmail'])
            data['speakerId'] = speaker.email
        del data['speakerName']
        del data['speakerEmail']
        del data['websafeKey']

        # # generate Session ID and key
        s_id = Session.allocate_ids(size=1, parent=c_key)[0]
        s_key = ndb.Key(Session, s_id, parent=c_key)
        data['key'] = s_key
        
        # create Session & return (modified) SessionForm
        session = Session(**data)
        session.put()
        taskqueue.add(params={'websafeConfKey': request.websafeConferenceKey,
            'speakerId': data['speakerId']},
            url='/tasks/check_featured_speaker'
        )
        return self._copySessionToForm(session)

    def _getConferenceSessions(self, request):
        """Given a conference, return a query object with its sessions"""
        # check that conference exists and get its details
        try:
            c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
            conf = c_key.get()
        except ProtocolBufferDecodeError:
            print('No conference found with key: %s' % request.websafeConferenceKey)
            raise

        # get sessions
        return Session.query(ancestor=c_key)
        
        

    @endpoints.method(CONF_GET_REQUEST, SessionForms,
            path='conference/{websafeConferenceKey}/sessions',
            http_method='GET', name='getConferenceSessions')
    def getConferenceSessions(self, request):
        """Given a conference, return all sessions as a query object."""
        # get sessions
        sessions = self._getConferenceSessions(request)
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )


    @endpoints.method(CONF_TYPE_GET_REQUEST, SessionForms,
            path='conference/{websafeConferenceKey}/{typeOfSession}/sessions',
            http_method='GET', name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request): 
        """Given a conference, return all sessions of a certain type"""
        # get all sessions
        sessions = self._getConferenceSessions(request)
        # filter only a certain type
        sessions = sessions.filter(Session.typeOfSession == request.typeOfSession)
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )


    @endpoints.method(SPEAKER_GET_REQUEST, SessionForms,
            path='speaker/{speakerId}/sessions',
            http_method='GET', name='getSessionsBySpeaker')
    def getSessionsBySpeaker(self, request):
        """Given a speaker, return all his sessions."""
        sessions = Session.query().filter(Session.speakerId == request.speakerId)
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )


    @endpoints.method(SESSION_POST_REQUEST, SessionForm, 
            path='/conference/{websafeConferenceKey}/session',
            http_method='POST', name='createSession')
    def createSession(self, request): 
        """Open only to the organizer of the conference"""
        return self._createSessionObject(request)



    def _updateSessionWishlist(self, request, add=True):
        """Add or remove session to/from wishlist"""
        prof = self._getProfileFromUser() # get user Profile
        
        # check if session exists given key and get it
        try:
            sessionKey = ndb.Key(urlsafe=request.sessionKey)
            session = sessionKey.get()
        except ProtocolBufferDecodeError:
            print('No session found with key: %s' % request.sessionKey)
            raise

        # add session
        if add:
            # check if session already added to wihslist, otherwise add
            if sessionKey in prof.sessionWishlist:
                raise ConflictException(
                    "You already added this session to your wishlist")

            prof.sessionWishlist.append(sessionKey)
            retval = True

        # remove session
        else:
            # check if session is in wishlist, if so remove
            if sessionKey in prof.sessionWishlist:
                prof.sessionWishlist.remove(sessionKey)
                retval = True
            else:
                retval = False

        # write profile back to datastore
        prof.put()

        return BooleanMessage(data=retval)



    @endpoints.method(WISHLIST_POST_REQUEST, BooleanMessage,
            path='conference/session/addToWishlist',
            http_method='POST', name='addToWishlist')
    def addToWishlist(self, request):
        """Add session to wishlist."""
        return self._updateSessionWishlist(request)


    @endpoints.method(WISHLIST_POST_REQUEST, BooleanMessage,
            path='conference/session/removeFromWishlist',
            http_method='DELETE', name='removeFromWishlist')
    def removeFromWishlist(self, request):
        """Remove session from wishlist."""
        return self._updateSessionWishlist(request, False)


    @endpoints.method(message_types.VoidMessage, SessionForms,
            path='getSessionsInWishlist', http_method='GET',
            name='getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """Get all the sessions from the current user's wishlist."""
        prof = self._getProfileFromUser() # get user Profile
        sessions = ndb.get_multi(prof.sessionWishlist)

        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )
          

    @staticmethod
    def _checkFeaturedSpeaker(websafeConfKey, speakerId):
        """Check if speaker should be featured (has more sessions
            in conf). If so, add to memcache with sessions"""
        # check that conference exists and get its key
        try:
            c_key = ndb.Key(urlsafe=websafeConfKey)
            conf = c_key.get()
        except ProtocolBufferDecodeError:
            print('No conference found with key: %s' % websafeConfKey)
            raise 

        # get sessions
        sessions = Session.query(ancestor=c_key)
        sessions = sessions.filter(Session.speakerId == speakerId)

        if sessions.count(limit=2) > 1:
            speakerNames = ", ".join(session.name for session in sessions)

            # get Speaker from datastore
            s_key = ndb.Key(Speaker, speakerId) 
            speaker = s_key.get()
            featuredSpeaker = "Featured speaker: " + speaker.name + \
                " with sessions " + speakerNames

            memcache.set(websafeConfKey, featuredSpeaker)
            return True

        return False
 

    @endpoints.method(CONF_GET_REQUEST, StringMessage,
            path='conference/{websafeConferenceKey}/getFeaturedSpeaker',
            http_method='GET', name='getFeaturedSpeaker')
    def getFeaturedSpeaker(self, request):
        """Return featured speaker from memcache."""
        return StringMessage(data=memcache.get(request.websafeConferenceKey) or "")


    @endpoints.method(message_types.VoidMessage, SessionForms,
            path='queryPlayground',
            http_method='GET', name='queryPlayground')
    def queryPlayground(self, request):
        """Query Playground"""
        q = Session.query()
        q = q.filter(Session.startTime < time(19, 0))
        q = q.filter(Session.typeOfSession.IN(["Lecture", "Keynote"]))
        
        
        return SessionForms(
            items=[self._copySessionToForm(session) for session in q]
        )


    @endpoints.method(CONF_IN_TOWN_REQUEST, ConferenceForms,
            path="getConfsInTownInInterval",
            http_method='GET', name='getConfsInTownInInterval')
    def getConfsInTownInInterval(self, request):
        """See what conferences are in a certain town and 
            are scheduled to start in a given time interval."""
        q = Conference.query()
        startDate = datetime.strptime(request.startDate[:10], "%Y-%m-%d").date()
        endDate = datetime.strptime(request.endDate[:10], "%Y-%m-%d").date()
        q = q.filter(Conference.city == request.city)
        q = q.filter(Conference.startDate >= startDate)
        q = q.filter(Conference.startDate <= endDate)
        
        return ConferenceForms(
            items = [self._copyConferenceToForm(conf, "") for conf in q]
        )


    @endpoints.method(CONF_POPULAR_REQUEST, ConferenceForms,
            path="getPopularConferences",
            http_method='GET', name='getPopularConferences')
    def getPopularConferences(self, request):
        """Get popular conferences on a given topic."""
        q = Conference.query()
        q = q.filter(Conference.topics == request.topic)
        q = q.filter(Conference.maxAttendees >= 100)
        q = q.filter(Conference.seatsAvailable.IN(range(1,20)))
        
        return ConferenceForms(
            items = [self._copyConferenceToForm(conf, "") for conf in q]
        )




api = endpoints.api_server([ConferenceApi]) # register API
