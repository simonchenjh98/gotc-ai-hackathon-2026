"""Run real Chromium against a temporary, loopback-only GOTC instance."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
from statistics import median
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from fixture import participants


@contextmanager
def local_event(source):
    if (source / "frontend").is_dir():
        sys.path.insert(0, str(source))
        from backend.gotc.http.server import EventHTTPServer
        from backend.gotc.service import EventService
        static = source / "frontend"
    else:
        sys.path.insert(0, str(source / "server"))
        from app import EventHTTPServer, EventService
        static = source

    with TemporaryDirectory(prefix="gotc-browser-") as data:
        service = EventService(data_dir=data, static_root=static, origin="http://127.0.0.1")
        with EventHTTPServer(("127.0.0.1", 0), service) as server:
            service.origin = f"http://127.0.0.1:{server.server_port}"
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                yield service
            finally:
                server.shutdown()
                worker.join()


def page_context(browser, service, **options):
    context = browser.new_context(service_workers="block", **options)
    failures = []
    def guard(route):
        url = urlsplit(route.request.url)
        if f"{url.scheme}://{url.netloc}" == service.origin:
            route.continue_()
        else:
            failures.append(f"unexpected outbound request: {url.path}")
            route.abort()
    context.route("**/*", guard)
    page = context.new_page()
    page.set_default_timeout(8000)
    page.on("pageerror", lambda error: failures.append(f"JavaScript: {error.name}: {error.message}"))
    page.on("response", lambda r: failures.append(f"HTTP {r.status}: {urlsplit(r.url).path}") if r.status >= 400 else None)
    page.on("console", lambda msg: failures.append("console: " + msg.text) if msg.type == "error" else None)
    page.on("requestfailed", lambda r: failures.append(f"{r.failure}: {urlsplit(r.url).path}")
            if r.failure != "net::ERR_ABORTED" or r.resource_type not in {"fetch", "xhr"} else None)
    return context, page, failures


def benchmark(browser, service, runs):
    samples = []
    scripts = None
    for _ in range(runs):
        context, page, failures = page_context(browser, service)
        try:
            cdp = context.new_cdp_session(page)
            cdp.send("Network.enable")
            cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
            cdp.send("Network.emulateNetworkConditions", {
                "offline": False, "latency": 40, "downloadThroughput": 200_000,
                "uploadThroughput": 100_000,
            })
            page.add_init_script("""(() => {
              const whenVisible = () => {
                const input = document.querySelector('#login-form input[name=code]');
                if (input?.getBoundingClientRect().width > 0) {
                  window.__loginVisibleMs = performance.now();
                } else requestAnimationFrame(whenVisible);
              };
              requestAnimationFrame(whenVisible);
            })()""")
            page.goto(service.origin + service.base_path + "/platform.html#team")
            page.locator("#login-form input[name=code]").wait_for()
            page.wait_for_load_state("networkidle")
            resources = page.evaluate(r"""() => performance.getEntriesByType('resource')
                .filter(r => /\.js$/.test(new URL(r.name).pathname))
                .map(r => [new URL(r.name).pathname, r.decodedBodySize])
                .sort()""")
            assert resources and all(size > 0 for _, size in resources), resources
            if scripts is not None:
                assert resources == scripts, "Script set changed between cold loads"
            scripts = resources
            samples.append(round(page.evaluate("window.__loginVisibleMs"), 1))
            assert not failures, failures
        finally:
            context.close()
    return {"loginVisibleMs": samples, "medianMs": median(samples),
            "decodedJavaScriptBytes": sum(size for _, size in scripts),
            "scripts": dict(scripts), "downloadBytesPerSecond": 200000,
            "latencyMs": 40, "coldContexts": runs}


def smoke(browser, service):
    ids = participants(service)
    base = service.origin + service.base_path + "/"
    passed = []

    @contextmanager
    def visit(role=None, **options):
        context, page, failures = page_context(browser, service, **options)
        try:
            if role:
                page.goto(base + "platform.html#" + role)
                form = page.locator("#login-form")
                form.locator("input[name=code]").fill(ids[role])
                with page.expect_response(lambda r: urlsplit(r.url).path.endswith("/api/login")) as login:
                    form.locator("button[type=submit]").click()
                assert login.value.status == 200, f"{role} login failed"
                form.wait_for(state="detached")
            yield page
            page.wait_for_load_state("networkidle")
            assert not failures, failures
        finally:
            context.close()

    def go(page, fragment, selector):
        page.evaluate("fragment => { location.hash = fragment; }", fragment)
        page.locator(selector).first.wait_for()

    def direct_entry(page, selector):
        page.reload(wait_until="networkidle")
        page.locator(selector).first.wait_for()

    with visit(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True) as page:
        page.goto(base)
        page.locator("[data-reg-search]").fill("Browser Fixture")
        page.get_by_role("heading", name="Browser Fixture Team").wait_for()
        page.locator("[data-reg-search]").fill("no matching team")
        page.locator(".registration-empty").wait_for()
        checklist = page.locator("input[data-check]").first
        checklist.check()
        assert "1 / 6" in page.locator("#check-count").inner_text()
        page.locator('a[href$="teams.html"]').filter(has_text="单独").click()
        page.locator("#registration-main").wait_for()
        page.get_by_role("heading", name="Browser Fixture Team").wait_for()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator('#registration-main a[href$="#claim"]').first.click()
        page.locator("#claim-request-form").wait_for()
        page.go_back()
        page.locator("#registration-main").wait_for()
        passed.append("mobile homepage checklist, directory search, public directory, claim link, browser Back")

    with visit() as page:
        page.goto(base + "platform.html#team")
        page.locator("#login-form").wait_for()
        go(page, "board-exhibition", "#board-content")
        go(page, "board-finals", 'main a[href="#team-score"]')
        go(page, "judge-roadshow", "[data-roadshow-name-login]")
        go(page, "judge-finals", "[data-finals-access]")
        finals = page.locator("[data-finals-access]")
        finals.locator("input[name=code]").fill(ids["finals"])
        finals.locator('button[type="submit"]').click()
        entry = page.locator("[data-finals-entry]")
        entry.locator("select").select_option(ids["seat"])
        entry.locator('button[type="submit"]').click()
        page.locator("[data-finals-score-form]").wait_for()
        page.locator("[data-finals-nomination-form]").wait_for()
        passed.append("signed-out contestant, exhibition board, closed finals board, roadshow entry, finals invitation and judge seat")
        go(page, "claim", "#claim-request-form")
        form = page.locator("#claim-request-form")
        for name, value in {"name": "Fixture Applicant", "school": "Fixture School", "teamName": "Fixture New Team"}.items():
            form.locator(f"[name={name}]").fill(value)
        for row, name in zip(form.locator("[data-claim-member-row]").all(), ["New One", "New Two"]):
            row.locator("[name=memberName]").fill(name)
            row.locator("[name=memberSchool]").fill("Fixture School")
        form.locator("button").filter(has_text="提交").last.click()
        page.locator("#claim-status").wait_for()
        page.locator("#claim-status").click()
        passed.append("contestant claim submission and receipt lookup")

    with visit("judge") as page:
        page.locator("#score-form").wait_for()
        go(page, "judge-exhibition", "#review-form")
        direct_entry(page, "#review-form")
        go(page, "judge-roadshow", "[data-roadshow-score-form]")
        direct_entry(page, "[data-roadshow-score-form]")
        go(page, "judge", "#score-form")
        passed.append("judge professional scoring, exhibition review and roadshow scoring")

    with visit("audience") as page:
        vote = page.locator("[data-vote]").first
        with page.expect_response(lambda r: urlsplit(r.url).path.endswith("/api/vote")) as result:
            vote.click()
        assert result.value.status == 200
        page.get_by_text("已支持 1 /", exact=False).first.wait_for()
        passed.append("audience invitation and vote persisted to the local API")

    with visit("team") as page:
        page.locator("#submission-form").wait_for(state="attached")
        page.locator("#team-keys").wait_for()
        go(page, "team-profile", "#team-profile-form")
        direct_entry(page, "#team-profile-form")
        assert page.locator('#team-profile-form input[name=name]').input_value() == "Browser Fixture Team"
        go(page, "team-score", "#refresh-team-score")
        direct_entry(page, "#refresh-team-score")
        page.locator("#refresh-team-score").click()
        page.locator("#refresh-team-score").wait_for()
        go(page, "team", "#jump-team-project-intro")
        passed.append("team submission workbench, AI panel, team profile and private score refresh")

    with visit("admin") as page:
        page.locator("#settings-form").wait_for()
        go(page, "admin-roadshow", "[data-roadshow-roster]")
        direct_entry(page, "[data-roadshow-roster]")
        go(page, "admin-finals", "[data-finals-roster]")
        direct_entry(page, "[data-finals-roster]")
        go(page, "admin-api", "#api-page")
        direct_entry(page, "#api-page")
        for section in ("accounts", "usage", "ops", "teams", "audit", "settings"):
            target = f'#admin-api/{section}'
            page.locator(f'.api-sidebar a[href="{target}"]').click()
            page.locator(f'.api-sidebar a[href="{target}"][aria-current=page]').wait_for()
        go(page, "team", "#admin-team-workbench")
        direct_entry(page, "#admin-team-workbench")
        form = page.locator("#admin-team-workbench")
        form.locator("select").select_option(ids["teamId"])
        form.locator("button").click()
        page.locator("#switch-team-workbench").wait_for()
        page.locator("#submission-form").wait_for(state="attached")
        page.locator("#switch-team-workbench").click()
        page.locator("#admin-team-workbench").wait_for()
        go(page, "admin", "#settings-form")
        passed.append("organizer dashboard, roadshow roster, finals roster, 7 AI console sections and delegated team round-trip")

    # Force a stale route to finish loading after the new route has rendered.
    with visit() as page:
        page.goto(base + "platform.html#team")
        page.locator("#login-form").wait_for()
        delayed = []
        page.route("**/claims/application.js", lambda route: delayed.append(route))
        with page.expect_request("**/claims/application.js"):
            page.evaluate("location.hash = 'claim'")
        go(page, "judge", "#login-form")
        assert len(delayed) == 1, "Claim module request did not start"
        with page.expect_event("requestfinished", predicate=lambda r: r.url.endswith("/claims/application.js")):
            delayed[0].continue_()
        page.evaluate("() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")
        assert page.locator("#login-form h2").inner_text() == "专业评委登录"
        assert page.locator("#claim-request-form").count() == 0
        go(page, "claim", "#claim-request-form")
        passed.append("delayed claim module cannot replace newer judge login; returning to claim succeeds")
    return passed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--chromium", help="Optional path to an existing Chromium executable")
    parser.add_argument("--runs", type=int, default=5)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--benchmark-only", action="store_true")
    mode.add_argument("--smoke-only", action="store_true")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    with local_event(args.source.resolve()) as service, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.chromium, args=["--disable-background-networking"])
        try:
            result = {"chromium": browser.version}
            if not args.smoke_only:
                result["coldLogin"] = benchmark(browser, service, args.runs)
            if not args.benchmark_only:
                result["smoke"] = smoke(browser, service)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
