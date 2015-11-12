App Engine application for the Udacity training course.

## Abstract
App derived from the Conference Central application provided.
The followin extensions were added:
- Add sessions to conferences
- Add sessions to user wishlist
- Add task to check for featured speakers

All of these were implemented both in the backend and in the frontend, 
so that the functionality could be more easily checked than just using
the API explorer.

## Products
- [App Engine][1]

## Language
- [Python][2]

## APIs
- [Google Cloud Endpoints][3]

## Setup Instructions
1. Update the value of `application` in `app.yaml` to the app ID you
   have registered in the App Engine admin console and would like to use to host
   your instance of this app.
1. Update the values at the top of `settings.py` to
   reflect the respective client IDs you have registered in the
   [Developer Console][4].
1. Update the value of CLIENT_ID in `static/js/app.js` to the Web client ID
1. Run the app with the devserver using `dev_appserver.py DIR`, and ensure it's 
	 running by visiting your local server's address (by default [localhost:8080][5].)
1. Deploy application.

## Task 1 - Add sessions to a conference
Sessions can have the following fields: 
- `name`
- `speakerId` with a Speaker data model that includes name, email, bio
- `duration` given in minutes
- `typeOfSession` can be `Workshop, Lecture, Keynote`
- `date` e.g. 2015-12-12
- `startTime` e.g. 11:30
- `location`

New sessions are created as __children__ of a specific conference.

When creating a new session, a user can enter either the name/email of a 
potentially new speaker, or choose from a list of already registered speakers.

Also, if a date is entered for the session, a check is made during validation
to ensure it corrsponds with the parent conference's start and end dates.

When accessing the details of a certain conference, a list of its sessions is
also displayed. Above that, there is a dropdown element which can be used to
display only a certain type of sessions. By default, all types are displayed.

## Task 2 - Add sessions to user wishlist

When accessing the details of a certain conference, a list of its sessions is
also displayed. For each of these sessions, a check is made to see if it
belongs to the current user's wishlist or not. Depending on this, a link showing
a `Add to wishlist` message or a `heart symbol` is displayed. Clicking this, 
will reverse the property by adding/removing the session from the wishlist.

The current wishlist can be seen by accessing the `Profile` tab.

## Task 3 - Work on indexes and queries.

Added 2 new queries:
- `getConfsInTownInInterval(city, startDate, endDate)` - See what conferences 
are in a certain town and are scheduled to start in a given time interval
- `getPopularConferences(topic)` - Get popular conferences on a given topic. By 
popular we understand conferences that can have at least 100 attendees and
have less than 20 seats left available.

Added given query:
- `queryPlayground()` - Get sessions that are not workshops and start before 7PM.
The obvious issue with this query is that the first solution most people would 
think of (filter for sessionType != Workshop and  startTime < 7PM) is not
compatible with Google's datastore, that does not accept 2 inequalities on
2 different fields in the same query. I chose to keep the time comparison
(startTime < 7PM) and test if the session type is any of the allowed ones 
(sessionType in [allowed values]).

## Task 4 - Add a task

When a new session is added to a conference, check the speaker. If there is more 
than one session by this speaker at this conference, also add a new Memcache 
entry that features the speaker and session names. 

The Memcache key is the conference's websafe key.

All of this is done using a task in App Engine's Task Queue.


[1]: https://developers.google.com/appengine
[2]: http://python.org
[3]: https://developers.google.com/appengine/docs/python/endpoints/
[4]: https://console.developers.google.com/
[5]: https://localhost:8080/
[6]: https://developers.google.com/appengine/docs/python/endpoints/endpoints_tool
