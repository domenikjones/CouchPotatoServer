from string import ascii_letters
import time
import traceback

from couchpotato import get_session
from couchpotato.core.event import addEvent, fireEventAsync, fireEvent
from couchpotato.core.helpers.encoding import toUnicode, simplifyString
from couchpotato.core.logger import CPLog
from couchpotato.core.media._base.library.base import LibraryBase
from qcond.helpers import simplify
from qcond import QueryCondenser


log = CPLog(__name__)

autload = 'SeasonLibraryPlugin'


class ShowLibraryPlugin(LibraryBase):

    default_dict = {'titles': {}, 'files':{}}

    def __init__(self):
        self.query_condenser = QueryCondenser()

        addEvent('library.query', self.query)
        addEvent('library.add.show', self.add)
        addEvent('library.update.show', self.update)
        addEvent('library.update.show_release_date', self.updateReleaseDate)

    def query(self, library, first = True, condense = True, **kwargs):
        if library is list or library.get('type') != 'show':
            return

        titles = [title['title'] for title in library['titles']]

        if condense:
            # Use QueryCondenser to build a list of optimal search titles
            condensed_titles = self.query_condenser.distinct(titles)

            if condensed_titles:
                # Use condensed titles if we got a valid result
                titles = condensed_titles
            else:
                # Fallback to simplifying titles
                titles = [simplify(title) for title in titles]

        if first:
            return titles[0] if titles else None

        return titles

    def add(self, attrs = {}, update_after = True):
        type = attrs.get('type', 'show')
        primary_provider = attrs.get('primary_provider', 'thetvdb')

        db = get_session()

        l = db.query(ShowLibrary).filter_by(type = type, identifier = attrs.get('identifier')).first()
        if not l:
            status = fireEvent('status.get', 'needs_update', single = True)
            l = ShowLibrary(
                type = type,
                primary_provider = primary_provider,
                year = attrs.get('year'),
                identifier = attrs.get('identifier'),
                plot = toUnicode(attrs.get('plot')),
                tagline = toUnicode(attrs.get('tagline')),
                status_id = status.get('id'),
                info = {},
                parent = None,
            )

            title = LibraryTitle(
                title = toUnicode(attrs.get('title')),
                simple_title = self.simplifyTitle(attrs.get('title')),
            )

            l.titles.append(title)

            db.add(l)
            db.commit()

        # Update library info
        if update_after is not False:
            handle = fireEventAsync if update_after is 'async' else fireEvent
            handle('library.update.show', identifier = l.identifier, default_title = toUnicode(attrs.get('title', '')))

        library_dict = l.to_dict(self.default_dict)
        db.expire_all()
        return library_dict

    def update(self, identifier, default_title = '', force = False):

        if self.shuttingDown():
            return

        db = get_session()
        library = db.query(ShowLibrary).filter_by(identifier = identifier).first()
        done_status = fireEvent('status.get', 'done', single = True)

        if library:
            library_dict = library.to_dict(self.default_dict)

        do_update = True

        info = fireEvent('show.info', merge = True, identifier = identifier)

        # Don't need those here
        try: del info['in_wanted']
        except: pass
        try: del info['in_library']
        except: pass

        if not info or len(info) == 0:
            log.error('Could not update, no show info to work with: %s', identifier)
            return False

        # Main info
        if do_update:
            library.plot = toUnicode(info.get('plot', ''))
            library.tagline = toUnicode(info.get('tagline', ''))
            library.year = info.get('year', 0)
            library.status_id = done_status.get('id')
            library.show_status = toUnicode(info.get('status',  '').lower())
            library.airs_time = info.get('airs_time', None)

            # Bits
            days_of_week_map =  {
                u'Monday':    1,
                u'Tuesday':   2,
                u'Wednesday': 4,
                u'Thursday':  8,
                u'Friday':    16,
                u'Saturday':  32,
                u'Sunday':    64,
                u'Daily':     127,
            }
            try:
                library.airs_dayofweek = days_of_week_map.get(info.get('airs_dayofweek'))
            except:
                library.airs_dayofweek = 0

            try:
                library.last_updated = int(info.get('lastupdated'))
            except:
                library.last_updated = int(time.time())

            library.info.update(info)

            db.commit()

            # Titles
            [db.delete(title) for title in library.titles]
            db.commit()

            titles = info.get('titles', [])
            log.debug('Adding titles: %s', titles)
            counter = 0
            for title in titles:
                if not title:
                    continue
                title = toUnicode(title)
                t = LibraryTitle(
                    title = title,
                    simple_title = self.simplifyTitle(title),
                    default = (len(default_title) == 0 and counter == 0) or len(titles) == 1 or title.lower() == toUnicode(default_title.lower()) or (toUnicode(default_title) == u'' and toUnicode(titles[0]) == title)
                )
                library.titles.append(t)
                counter += 1

            db.commit()

            # Files
            images = info.get('images', [])
            for image_type in ['poster']:
                for image in images.get(image_type, []):
                    if not isinstance(image, (str, unicode)):
                        continue

                    file_path = fireEvent('file.download', url = image, single = True)
                    if file_path:
                        file_obj = fireEvent('file.add', path = file_path, type_tuple = ('image', image_type), single = True)
                        try:
                            file_obj = db.query(File).filter_by(id = file_obj.get('id')).one()
                            library.files.append(file_obj)
                            db.commit()

                            break
                        except:
                            log.debug('Failed to attach to library: %s', traceback.format_exc())

            library_dict = library.to_dict(self.default_dict)

        db.expire_all()
        return library_dict