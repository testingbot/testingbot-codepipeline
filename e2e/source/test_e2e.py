"""Two sessions through the tunnel. E2E_FAIL (a file in the source zip) makes
the second one report a failure, to exercise the gate's failing path."""
import base64, os, urllib.parse, urllib.request
from selenium import webdriver

KEY, SECRET = os.environ["TESTINGBOT_KEY"], os.environ["TESTINGBOT_SECRET"]
EXPECTED = f"tb-e2e-{os.environ['CODEBUILD_BUILD_NUMBER']}"
FAIL = os.path.exists("E2E_FAIL")


def report(session_id, success, message):
    data = urllib.parse.urlencode({"test[success]": "1" if success else "0", "test[status_message]": message}).encode()
    req = urllib.request.Request(f"https://api.testingbot.com/v1/tests/{session_id}", data=data, method="PUT")
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{KEY}:{SECRET}".encode()).decode())
    urllib.request.urlopen(req, timeout=30).read()


SESSIONS = (("tunnel page loads", True), ("deliberate failure" if FAIL else "tunnel page again", not FAIL))

for n, (name, should_pass) in enumerate(SESSIONS, 1):
    options = webdriver.ChromeOptions()
    options.set_capability("platformName", "WIN11")
    options.set_capability("browserVersion", "latest")
    options.set_capability("tb:options", {
        "key": KEY, "secret": SECRET, "name": name,
        "build": os.environ["TESTINGBOT_BUILD"],
        "tunnel-identifier": os.environ["TESTINGBOT_TUNNEL_IDENTIFIER"],
    })
    driver = webdriver.Remote("https://hub.testingbot.com/wd/hub", options=options)
    try:
        # A distinct URL per session, so a cache between the two cannot hide a
        # tunnel problem (or cause one).
        driver.get(f"http://localhost:8080/?session={n}")
        title = driver.title
        print(f"  url={driver.current_url!r} body={driver.find_element('tag name', 'body').text[:200]!r}")
        ok = title == EXPECTED and should_pass
        msg = f"title={title!r}" + ("" if should_pass else " (E2E_FAIL set)")
        print(f"{name}: {'PASS' if ok else 'FAIL'} {msg}")
        report(driver.session_id, ok, msg)
    finally:
        driver.quit()
    if title != EXPECTED:
        raise SystemExit(f"tunnel did not serve the build's page: {title!r}")
