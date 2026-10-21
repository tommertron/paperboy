// Paperboy: settings card for the reader's web UI.
//
// The on-reader plugin (device.json) talks to a Paperboy server and reads its
// address and token from CONFIG_PATH. This card is where you enter them, from a
// phone or computer, so no one has to edit JSON on the SD card.
//
// CONFIG_PATH lives in /.crosspoint/plugin-data/, which Plugin Hub never
// touches on update or uninstall, so the settings survive both. Credentials are
// never part of the published plugin files.
CrossPoint.registerPlugin(async (container, api) => {
  const PLUGIN_ID = 'paperboy';
  const CONFIG_PATH = '/.crosspoint/plugin-data/' + PLUGIN_ID + '/config.json';
  const PLUGIN_DIR = api && typeof api.dir === 'string' ? api.dir.replace(/\/+$/, '') : '';
  // Older places these settings lived: inside the plugin folder, and under the
  // plugin's previous name ("instapaper"). The first one found is moved here.
  const LEGACY_PATHS = [
    PLUGIN_DIR ? PLUGIN_DIR + '/config.json' : '',
    '/.crosspoint/plugin-data/instapaper/config.json',
    '/plugins/instapaper/config.json',
    '/.crosspoint/plugins/instapaper/config.json',
  ].filter(Boolean);

  container.innerHTML =
    '<h2>Paperboy</h2>' +
    '<p style="color:#666">Your Paperboy server. The reader uses these settings to list, ' +
    'download and archive articles. They are stored on this reader\'s SD card only.</p>' +
    '<div class="setting-row"><span class="setting-name">Server URL</span>' +
    '<span class="setting-control"><input type="url" id="rl-server" placeholder="https://your-server.example.com/paperboy" style="width:100%"></span></div>' +
    '<div class="setting-row"><span class="setting-name">Token</span>' +
    '<span class="setting-control"><input type="password" id="rl-token" autocomplete="off" style="width:100%"> ' +
    '<button type="button" class="btn-small" id="rl-show">Show</button></span></div>' +
    '<div class="setting-row">' +
    '<button type="button" class="btn-small" id="rl-test">Test connection</button> ' +
    '<button type="button" class="btn-small btn-add" id="rl-save">Save</button></div>' +
    '<p id="rl-status"></p>';

  const $ = (id) => document.getElementById(id);
  const status = (msg, bad) => {
    $('rl-status').textContent = msg;
    $('rl-status').style.color = bad ? '#bc3636' : '';
  };

  function b64(text) {
    const bytes = new TextEncoder().encode(text);
    let bin = '';
    for (const b of bytes) bin += String.fromCharCode(b);
    return btoa(bin);
  }

  async function readJson(path) {
    if (!path) return null;
    try {
      const r = await fetch('/download?path=' + encodeURIComponent(path));
      return r.ok ? await r.json() : null;
    } catch (e) {
      return null;
    }
  }

  function values() {
    return {
      server: $('rl-server').value.trim().replace(/\/+$/, ''),
      token: $('rl-token').value.trim(),
    };
  }

  function validate(v) {
    if (!/^https?:\/\/[^\s/]+/i.test(v.server)) return 'Enter the server URL, starting with https://';
    if (!v.token) return 'Enter the token';
    return '';
  }

  async function save(v) {
    // dest_dir "/" puts bundle downloads (subdir "Instapaper") in /Instapaper.
    const body = JSON.stringify({ server: v.server, token: v.token, dest_dir: '/' }, null, 2);
    const res = await api.writeFile(CONFIG_PATH, b64(body));
    if (!res || res.ok === false) throw new Error('could not write ' + CONFIG_PATH);
  }

  // Load existing settings, migrating a legacy in-folder config.json once.
  let cfg = await readJson(CONFIG_PATH);
  for (const path of cfg ? [] : LEGACY_PATHS) {
    const legacy = await readJson(path);
    if (!legacy || !legacy.server || !legacy.token) continue;
    try {
      await save({ server: legacy.server, token: legacy.token });
      cfg = legacy;
      status('Copied your settings from ' + path + '. Tap Test connection to check them.');
    } catch (e) {
      status('Found settings in ' + path + ' but could not copy them: ' + e.message, true);
    }
    break;
  }
  if (cfg) {
    $('rl-server').value = cfg.server || '';
    $('rl-token').value = cfg.token || '';
  } else {
    status('Not set up yet. Enter your server URL and token, then Save.');
  }

  $('rl-show').onclick = () => {
    const t = $('rl-token');
    t.type = t.type === 'password' ? 'text' : 'password';
    $('rl-show').textContent = t.type === 'password' ? 'Show' : 'Hide';
  };

  $('rl-test').onclick = async () => {
    const v = values();
    const problem = validate(v);
    if (problem) return status(problem, true);
    status('Testing…');
    try {
      const health = await api.relay('GET', v.server + '/health', {}, '');
      if (health.status !== 200) return status('Server did not answer /health (HTTP ' + health.status + ').', true);
      const lists = await api.relay('GET', v.server + '/lists?token=' + encodeURIComponent(v.token), {}, '');
      if (lists.status === 401) return status('Server is up, but it rejected the token.', true);
      if (lists.status !== 200) return status('Server is up, but listing failed (HTTP ' + lists.status + ').', true);
      const names = JSON.parse(lists.body).lists.map((l) => l.title).join(', ');
      status('Connected. Lists: ' + names + '.');
    } catch (e) {
      status('Could not reach the server: ' + e.message, true);
    }
  };

  $('rl-save').onclick = async () => {
    const v = values();
    const problem = validate(v);
    if (problem) return status(problem, true);
    try {
      await save(v);
      status('Saved. Restart the reader so finished-article reports use the new settings.');
    } catch (e) {
      status('Save failed: ' + e.message, true);
    }
  };
});
