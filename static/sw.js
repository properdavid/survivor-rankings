// Minimal service worker — enables "Add to Home Screen" on Android.
// No offline caching; all requests pass through to the network.
self.addEventListener("fetch", () => {});
