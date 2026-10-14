// Read, change or delete tracks by persistent ID.
// usage: osascript -l JavaScript track.js COMMAND IN.json
//   info    IN = ["PID", ...]                 -> [{pid, location, duration, ...} | null, ...]
//   set     IN = {"PID": {"name": "...", ...}} -> {"ok": n, "errors": [...]}
//   delete  IN = ["PID", ...]                 -> {"ok": n, "errors": [...]}
ObjC.import('Foundation');

function readJSON(path) {
  const s = $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null);
  return JSON.parse(s.js);
}

function run(argv) {
  const cmd = argv[0];
  const data = readJSON(argv[1]);
  const lib = Application('Music').libraryPlaylists[0].fileTracks;
  const find = (pid) => {
    const hits = lib.whose({persistentID: pid})();
    return hits.length ? hits[0] : null;
  };
  if (cmd === 'info') {
    return JSON.stringify(data.map((pid) => {
      const t = find(pid);
      if (!t) return null;
      let loc = null;
      try { loc = t.location(); } catch (e) { loc = null; }
      return {pid: pid, location: loc ? loc.toString() : null, duration: t.duration(),
              name: t.name(), artist: t.artist(), album: t.album(), lyrics: t.lyrics() || '',
              plays: t.playedCount()};
    }));
  }
  let ok = 0;
  const errors = [];
  if (cmd === 'set') {
    for (const pid in data) {
      try {
        const t = find(pid);
        if (!t) throw new Error('not found');
        const props = data[pid];
        for (const k in props) t[k] = props[k];
        ok++;
      } catch (e) { errors.push(pid + ': ' + e); }
    }
  } else if (cmd === 'delete') {
    for (const pid of data) {
      try {
        const t = find(pid);
        if (!t) throw new Error('not found');
        t.delete();
        ok++;
      } catch (e) { errors.push(pid + ': ' + e); }
    }
  } else {
    throw new Error('unknown command: ' + cmd);
  }
  return JSON.stringify({ok: ok, errors: errors});
}
