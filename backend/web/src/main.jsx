import React from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

const arrows = [
  { key: 'UP', label: 'Up', symbol: '↑', position: 'up' },
  { key: 'LEFT', label: 'Left', symbol: '←', position: 'left' },
  { key: 'ENTER', label: 'OK, select', symbol: 'OK', position: 'center' },
  { key: 'RIGHT', label: 'Right', symbol: '→', position: 'right' },
  { key: 'DOWN', label: 'Down', symbol: '↓', position: 'down' },
];

function RemoteButton({ command, onSend, children, className = '', disabled = false }) {
  return (
    <button
      className={`remote-button ${className}`}
      type="button"
      aria-label={command.label}
      onClick={() => onSend(command.key)}
      disabled={disabled}
    >
      {children}
    </button>
  );
}

function App() {
  const [status, setStatus] = React.useState({ text: 'Ready when you are', kind: 'ready' });
  const [sending, setSending] = React.useState(false);
  const [padMode, setPadMode] = React.useState('directions');
  const [devices, setDevices] = React.useState([]);
  const [extraKeys, setExtraKeys] = React.useState([]);
  const [selectedDeviceId, setSelectedDeviceId] = React.useState('');
  const [recording, setRecording] = React.useState(false);
  const [recordedActions, setRecordedActions] = React.useState([]);
  const [recordingLoaded, setRecordingLoaded] = React.useState(false);
  const recordingReady = React.useRef(false);
  const [happySeconds, setHappySeconds] = React.useState(0);
  const [confirmAction, setConfirmAction] = React.useState(null);
  const [wifiOpen, setWifiOpen] = React.useState(false);
  const [wifiSsid, setWifiSsid] = React.useState('');
  const [wifiPassword, setWifiPassword] = React.useState('');
  const [wifiShowPassword, setWifiShowPassword] = React.useState(false);
  const [wifiSubmitting, setWifiSubmitting] = React.useState(false);
  const [wifiError, setWifiError] = React.useState('');
  const happyLocked = React.useRef(false);

  React.useEffect(() => {
    if (!wifiOpen) return undefined;
    function closeOnEscape(event) {
      if (event.key === 'Escape') closeWifi();
    }
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [wifiOpen]);

  React.useEffect(() => {
    let active = true;
    // Extra buttons (Home, volume, ...) are defined on the server in backend/keys.json.
    fetch('/api/keys')
      .then((response) => response.ok ? response.json() : { keys: [] })
      .then((result) => { if (active && Array.isArray(result.keys)) setExtraKeys(result.keys); })
      .catch(() => {});
    return () => { active = false; };
  }, []);

  React.useEffect(() => {
    let active = true;
    fetch('/api/recording')
      .then(async (response) => {
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Could not load recording');
        if (active) {
          setRecordedActions(Array.isArray(result.commands) ? result.commands : []);
          recordingReady.current = true;
          setRecordingLoaded(true);
        }
      })
      .catch((error) => {
        if (active) setStatus((current) => current.kind === 'error' ? current : { text: `Could not load recording: ${error.message}`, kind: 'error' });
      });
    return () => { active = false; };
  }, []);

  React.useEffect(() => {
    if (!recordingReady.current) return undefined;
    const timer = window.setTimeout(() => {
      fetch('/api/recording', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ commands: recordedActions }),
      }).then((response) => {
        if (!response.ok) throw new Error('Could not save recording');
      }).catch((error) => {
        setStatus((current) => current.kind === 'error' ? current : { text: error.message, kind: 'error' });
      });
    }, 200);
    return () => window.clearTimeout(timer);
  }, [recordedActions]);

  React.useEffect(() => {
    if (happySeconds <= 0) return undefined;
    const timer = window.setInterval(() => setHappySeconds((seconds) => Math.max(0, seconds - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [happySeconds > 0]);

  React.useEffect(() => {
    if (happySeconds === 0) happyLocked.current = false;
  }, [happySeconds]);

  React.useEffect(() => {
    if (!confirmAction) return undefined;
    function closeOnEscape(event) {
      if (event.key === 'Escape') setConfirmAction(null);
    }
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [confirmAction]);

  React.useEffect(() => {
    let active = true;
    async function refreshDevices() {
      try {
        const response = await fetch('/api/devices');
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Could not load devices');
        if (!active) return;
        const nextDevices = Array.isArray(result.devices) ? result.devices : [];
        setDevices(nextDevices);
        setSelectedDeviceId((current) => nextDevices.some((device) => device.device_id === current)
          ? current
          : (nextDevices[0]?.device_id || ''));
      } catch (error) {
        if (active) setStatus((current) => current.kind === 'error' ? current : { text: 'Could not load remotes', kind: 'error' });
      }
    }
    refreshDevices();
    const timer = window.setInterval(refreshDevices, 1500);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  async function sendKey(key) {
    if (!selectedDeviceId) return;
    if (recording) {
      setRecordedActions((actions) => [...actions, { device_id: selectedDeviceId, key }].slice(-100));
    }
    setSending(true);
    setStatus({ text: `Sending ${key === 'ENTER' ? 'OK' : key.toLowerCase()}…`, kind: 'busy' });
    try {
      const response = await fetch('/api/commands', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: selectedDeviceId, key }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Request failed');
      setStatus({ text: `${key === 'ENTER' ? 'OK' : key.toLowerCase()} queued`, kind: 'success' });
    } catch (error) {
      setStatus({ text: `Could not send: ${error.message}`, kind: 'error' });
    } finally {
      setSending(false);
    }
  }

  function toggleRecording() {
    if (!recording) setRecordedActions([]);
    setRecording((active) => !active);
    setStatus(recording
      ? { text: `Recording saved · ${recordedActions.length} actions`, kind: 'success' }
      : { text: 'Recording actions from either remote', kind: 'busy' });
  }

  async function playRecording() {
    if (happyLocked.current || !recordingLoaded || !recordedActions.length) return;
    happyLocked.current = true;
    setHappySeconds(60);
    setStatus({ text: `Playing ${recordedActions.length} recorded actions`, kind: 'busy' });
    try {
      const response = await fetch('/api/playback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ commands: recordedActions }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Could not start playback');
      setStatus({ text: `Playing ${result.actions} actions across your remotes`, kind: 'success' });
    } catch (error) {
      happyLocked.current = false;
      setHappySeconds(0);
      setStatus({ text: `Could not play: ${error.message}`, kind: 'error' });
    }
  }

  function closeWifi() {
    setWifiOpen(false);
    setWifiSsid('');
    setWifiPassword('');       // never keep the password around after the overlay closes
    setWifiShowPassword(false);
    setWifiError('');
  }

  async function submitWifi(event) {
    event.preventDefault();
    if (!selectedDeviceId || wifiSubmitting) return;
    setWifiSubmitting(true);
    setWifiError('');
    try {
      const response = await fetch('/api/wifi', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: selectedDeviceId, ssid: wifiSsid, password: wifiPassword }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Could not send the Wi-Fi settings');
      setWifiPassword('');
      setWifiShowPassword(false);
    } catch (error) {
      setWifiError(error.message);
    } finally {
      setWifiSubmitting(false);
    }
  }

  const back = { key: 'BACK', label: 'Back (Escape)' };
  const xiaomiBack = { key: 'BACK_XIAOMI', label: 'Xiaomi Back (usage F1)' };
  const selectedDevice = devices.find((device) => device.device_id === selectedDeviceId);
  const remoteDisabled = !selectedDeviceId || sending;
  return (
    <main className="remote-shell">
      <header className="device-header">
        <nav className="device-tabs" role="tablist" aria-label="Registered remotes">
          {devices.map((device) => (
            <button
              key={device.device_id}
              id={`tab-${device.device_id}`}
              className={`device-tab${device.device_id === selectedDeviceId ? ' selected' : ''}`}
              type="button"
              role="tab"
              aria-selected={device.device_id === selectedDeviceId}
              aria-controls="remote-panel"
              aria-haspopup="dialog"
              title={device.device_id === selectedDeviceId ? 'Wi-Fi settings' : undefined}
              onClick={() => {
                if (device.device_id === selectedDeviceId) { setWifiOpen(true); return; }
                setSelectedDeviceId(device.device_id);
                setStatus({ text: 'Ready when you are', kind: 'ready' });
              }}
            >
              <span className={`device-dot${device.online ? ' online' : ''}`} aria-hidden="true" />
              <span className="device-name">{device.name || device.device_id}</span>
              {device.device_id === selectedDeviceId && <span className="device-gear" aria-hidden="true">⚙</span>}
            </button>
          ))}
          {devices.length === 0 && <span className="device-empty" role="status">Waiting for ESP32…</span>}
        </nav>
      </header>

      <section id="remote-panel" role="tabpanel" aria-labelledby={selectedDevice ? `tab-${selectedDevice.device_id}` : undefined}>
        <p className={`status status-${status.kind}`} role="status" aria-live="polite">
          <span className="status-dot" aria-hidden="true" />{selectedDevice ? status.text : 'Flash and connect an ESP32 to register a remote'}
        </p>

        <section className="controls" aria-label="Navigation controls">
        <div className="pad-switcher">
          <div className="pad-heading">
            <h2 id="pad-heading">{padMode === 'directions' ? 'Navigation' : 'Number pad'}</h2>
            <button
              type="button"
              className="pad-toggle"
              aria-label={`Show ${padMode === 'directions' ? 'number pad' : 'navigation pad'}`}
              aria-pressed={padMode === 'numbers'}
              onClick={() => setPadMode((mode) => mode === 'directions' ? 'numbers' : 'directions')}
            >
              <span aria-hidden="true">⟳</span> {padMode === 'directions' ? 'Numbers' : 'Arrows'}
            </button>
          </div>
          {padMode === 'directions' ? (
            <div className="direction-pad" aria-labelledby="pad-heading">
              {arrows.map((command) => (
                <RemoteButton key={command.key} command={command} onSend={sendKey} className={`direction ${command.position}`} disabled={remoteDisabled}>
                  {command.symbol}
                </RemoteButton>
              ))}
              <button type="button" className={`record-button${recording ? ' is-recording' : ''}`} onClick={() => setConfirmAction(recording ? 'stop-recording' : 'start-recording')} aria-pressed={recording} disabled={!recordingLoaded || remoteDisabled}>
                <span className="record-dot" aria-hidden="true" />{recording ? 'Stop' : 'Record'}
                <span className="recording-count">{recordedActions.length} {recordedActions.length === 1 ? 'action' : 'actions'}</span>
              </button>
            </div>
          ) : (
            <div className="number-pad shared-pad" aria-labelledby="pad-heading">
              {[1, 2, 3, 4, 5, 6, 7, 8, 9, '', 0, ''].map((number, index) => number === ''
                ? <span className="number-spacer" key={`space-${index}`} aria-hidden="true" />
                : <RemoteButton key={number} command={{ key: String(number), label: `Number ${number}` }} onSend={sendKey} className="number-button" disabled={remoteDisabled}>{number}</RemoteButton>)}
            </div>
          )}
        </div>
        <div className="back-buttons">
          <RemoteButton command={back} onSend={sendKey} className="back-button" disabled={remoteDisabled}>
            <span className="back-symbol" aria-hidden="true">↶</span>Back
          </RemoteButton>
          <RemoteButton command={xiaomiBack} onSend={sendKey} className="back-button xiaomi-back-button" disabled={remoteDisabled}>
            <span className="back-symbol" aria-hidden="true">↶</span>Xiaomi Back · F1
          </RemoteButton>
        </div>
        {extraKeys.some((key) => key.group === 'extra') && <div className="media-buttons" role="group" aria-label="TV controls">
          {extraKeys.filter((key) => key.group === 'extra').map((command) => (
            <RemoteButton key={command.key} command={command} onSend={sendKey} className="media-button" disabled={remoteDisabled}>
              <span className="media-symbol" aria-hidden="true">{command.symbol}</span>{command.label}
            </RemoteButton>
          ))}
        </div>}
        </section>

        <section className="happy-section" aria-label="Happy button">
        <button type="button" className="happy-button" aria-label={happySeconds ? `Playing recording, ${happySeconds} seconds remaining` : 'Play recorded actions'} onClick={() => setConfirmAction('playback')} disabled={!recordingLoaded || happySeconds > 0 || !recordedActions.length}>
          <img src="/Happy.png" alt="Happy" />
          {happySeconds > 0 && <span className="happy-timer" aria-hidden="true">
            <svg viewBox="0 0 100 100"><circle className="timer-track" cx="50" cy="50" r="45" /><circle className="timer-progress" cx="50" cy="50" r="45" style={{ strokeDashoffset: `${283 * (1 - happySeconds / 60)}` }} /></svg>
            <span className="timer-label">{happySeconds}</span>
          </span>}
        </button>
        </section>
      </section>
      {wifiOpen && selectedDevice && (() => {
        const wifi = selectedDevice.wifi;
        const busy = wifiSubmitting || (wifi && (wifi.status === 'pending' || wifi.status === 'delivered'));
        const passwordTooShort = wifiPassword.length > 0 && wifiPassword.length < 8;
        return (
          <div className="confirm-backdrop" onClick={(event) => { if (event.target === event.currentTarget) closeWifi(); }}>
            <form className="confirm-dialog wifi-dialog" role="dialog" aria-modal="true" aria-labelledby="wifi-title" onSubmit={submitWifi}>
              <h2 id="wifi-title">Wi-Fi for {selectedDevice.name || selectedDevice.device_id}</h2>
              <p className="wifi-help">The remote tries the new network first and keeps its current one if it cannot connect.</p>
              <label className="wifi-field">Network name
                <input type="text" value={wifiSsid} onChange={(event) => setWifiSsid(event.target.value)} maxLength={32}
                  autoComplete="off" autoCapitalize="off" spellCheck="false" autoFocus required disabled={busy} />
              </label>
              <label className="wifi-field">Password <span className="wifi-optional">(empty for an open network)</span>
                <span className="wifi-password-row">
                  <input type={wifiShowPassword ? 'text' : 'password'} value={wifiPassword} onChange={(event) => setWifiPassword(event.target.value)}
                    maxLength={63} autoComplete="new-password" spellCheck="false" disabled={busy} />
                  <button type="button" className="wifi-show" aria-pressed={wifiShowPassword} onClick={() => setWifiShowPassword((shown) => !shown)}>{wifiShowPassword ? 'Hide' : 'Show'}</button>
                </span>
              </label>
              {passwordTooShort && <p className="wifi-message wifi-warning" role="status">Passwords need at least 8 characters.</p>}
              {wifiError && <p className="wifi-message wifi-error" role="alert">{wifiError}</p>}
              {wifi && (
                <p className={`wifi-message wifi-${wifi.status}`} role="status" aria-live="polite">
                  {wifi.status === 'pending' && `Waiting for the remote to pick up “${wifi.ssid}”…`}
                  {wifi.status === 'delivered' && `Remote is trying “${wifi.ssid}”…`}
                  {wifi.status === 'applied' && `Connected to “${wifi.ssid}”.`}
                  {wifi.status === 'failed' && (wifi.error || 'The change did not work.')}
                </p>
              )}
              {!selectedDevice.online && <p className="wifi-message wifi-warning" role="status">This remote is offline; the change is applied when it next connects.</p>}
              <div className="confirm-actions">
                <button type="button" className="confirm-cancel" onClick={closeWifi}>Close</button>
                <button type="submit" className="confirm-yes" disabled={busy || !wifiSsid || passwordTooShort}>{busy ? 'Working…' : 'Change Wi-Fi'}</button>
              </div>
            </form>
          </div>
        );
      })()}
      {confirmAction && (
        <div className="confirm-backdrop" onClick={(event) => { if (event.target === event.currentTarget) setConfirmAction(null); }}>
          <section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
            <h2 id="confirm-title"><img className="confirm-title-image" src="/Cena.png" alt="Are you sure about that?" /></h2>
            <p>{confirmAction === 'playback'
              ? `Play the ${recordedActions.length} recorded ${recordedActions.length === 1 ? 'action' : 'actions'} now?`
              : confirmAction === 'start-recording'
                ? 'Start recording actions from both remotes? This will replace the current recording.'
                : 'Stop recording and save the actions captured so far?'}</p>
            <div className="confirm-actions">
              <button type="button" className="confirm-cancel" autoFocus onClick={() => setConfirmAction(null)}>Cancel</button>
              <button type="button" className="confirm-yes" onClick={() => {
                const action = confirmAction;
                setConfirmAction(null);
                if (action === 'playback') playRecording();
                else toggleRecording();
              }}>{confirmAction === 'playback' ? 'Yes, play' : confirmAction === 'start-recording' ? 'Start recording' : 'Stop and save'}</button>
            </div>
          </section>
        </div>
      )}
      {sending && <span className="sr-only">Sending command</span>}
    </main>
  );
}

createRoot(document.getElementById('root')).render(<App />);
