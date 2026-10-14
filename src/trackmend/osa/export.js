// Export the file tracks of the library (or of one playlist) as JSON.
// usage: osascript -l JavaScript export.js OUT.json [PLAYLIST]
ObjC.import('Foundation');

function run(argv) {
  const app = Application('Music');
  let src;
  if (argv.length > 1 && argv[1]) {
    const pls = app.userPlaylists.whose({name: argv[1]})();
    if (pls.length === 0) throw new Error('playlist not found: ' + argv[1]);
    src = pls[0].fileTracks;
  } else {
    src = app.libraryPlaylists[0].fileTracks;
  }
  const get = (p) => { try { return src[p](); } catch (e) { return null; } };
  const ids = get('persistentID') || [];
  const props = ['name', 'artist', 'album', 'albumArtist', 'year', 'genre', 'duration', 'location',
                 'playedCount', 'bitRate', 'lyrics', 'trackNumber', 'dateAdded', 'kind'];
  const col = {};
  for (const p of props) col[p] = get(p);
  col.favorited = get('favorited') || get('loved');
  const out = [];
  for (let i = 0; i < ids.length; i++) {
    const v = (p) => (col[p] ? col[p][i] : null);
    const loc = v('location');
    const added = v('dateAdded');
    out.push({
      pid: ids[i], name: v('name') || '', artist: v('artist') || '', album: v('album') || '',
      album_artist: v('albumArtist') || '', year: v('year') || 0, genre: v('genre') || '',
      duration: v('duration') || 0, location: loc ? loc.toString() : null,
      plays: v('playedCount') || 0, bit_rate: v('bitRate') || 0,
      lyrics_len: (v('lyrics') || '').length, track_number: v('trackNumber') || 0,
      date_added: added ? added.toISOString() : '', kind: v('kind') || '',
      favorited: !!v('favorited'),
    });
  }
  const s = $.NSString.alloc.initWithUTF8String(JSON.stringify(out));
  s.writeToFileAtomicallyEncodingError(argv[0], true, $.NSUTF8StringEncoding, null);
  return String(out.length);
}
