import asyncio
import sys


def configure_playwright_event_loop_policy():
    """Use a subprocess-capable asyncio policy for Playwright on Windows."""
    if not sys.platform.startswith("win"):
        return
    policy_factory = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if policy_factory is None:
        return
    if asyncio.get_event_loop_policy().__class__ is not policy_factory:
        asyncio.set_event_loop_policy(policy_factory())


def start_chromium_browser(headless=True):
    configure_playwright_event_loop_policy()
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(headless=headless)
    except Exception:
        playwright.stop()
        raise
    return playwright, browser


def install_browser_test_stubs(context):
    """Provide local browser globals normally loaded from CDN UI libraries."""
    context.add_init_script(
        """
        (() => {
          window.lucide = window.lucide || { createIcons: () => {} };
          if (!window.bootstrap) {
            class Modal {
              constructor() {}
              show() {}
              hide() {}
              static getInstance() { return new Modal(); }
              static getOrCreateInstance() { return new Modal(); }
            }
            window.bootstrap = { Modal };
          }
          if (!window.L) {
            const chainableLayer = {
              addTo() { return this; },
              bindPopup() { return this; },
              remove() { return this; },
            };
            window.L = {
              map(id) {
                const element = typeof id === 'string' ? document.getElementById(id) : id;
                return {
                  on(eventName, callback) {
                    if (eventName === 'click' && element) {
                      element.addEventListener('click', () => callback({
                        latlng: { lat: 48.8584, lng: 2.2945 },
                      }));
                    }
                    return this;
                  },
                  off() { return this; },
                  removeLayer() { return this; },
                  invalidateSize() { return this; },
                  fitBounds() { return this; },
                  setView() { return this; },
                  remove() { return this; },
                };
              },
              tileLayer() { return chainableLayer; },
              marker() { return { ...chainableLayer }; },
              icon(options) { return options || {}; },
              polyline() { return { ...chainableLayer }; },
              latLngBounds(bounds) { return bounds || []; },
            };
          }
        })();
        """
    )
