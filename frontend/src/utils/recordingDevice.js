// This page's recording key (#388). A recording left behind by one device
// or tab can be continued on another ("Continue recording"); the server
// then refuses whatever the first one still sends (chunks, finalize,
// release, its transcription stream) with `session_taken_over`, so two
// devices never record into one session. The key is sent when this page
// starts or continues a recording, and with everything it sends for it.
//
// One key per page load: two tabs are two recorders, and a reloaded tab no
// longer has the recording its previous page made.

function makeKey() {
  try {
    // getRandomValues, not randomUUID: it also works on plain http (a phone
    // testing against a LAN address).
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  } catch (_) {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }
}

export const RECORDING_DEVICE_KEY = makeKey();

// The server's code for a request from a device whose recording another
// device continued.
export const TAKEN_OVER_CODE = 'session_taken_over';
