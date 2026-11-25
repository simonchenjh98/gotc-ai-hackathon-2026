"""Contestant file-picker and submission journey in the disposable browser event."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from zipfile import ZipFile

from playwright.sync_api import expect


def empty_presentation(path):
    """A minimal Open Packaging Conventions presentation with no slides."""
    with ZipFile(path, "w") as deck:
        deck.writestr("[Content_Types].xml", """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
          <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
          <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
        </Types>""")
        deck.writestr("_rels/.rels", """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
        </Relationships>""")
        deck.writestr("ppt/presentation.xml", """<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
          <p:notesSz cx="6858000" cy="9144000"/>
        </p:presentation>""")


def set_window(admin, enabled):
    settings = admin.locator("#submission-settings")
    settings.locator("[name=submissionsOpen]").set_checked(enabled)
    with admin.expect_response(lambda r: urlsplit(r.url).path.endswith("/api/admin/settings")) as update:
        settings.get_by_role("button", name="保存提交窗口").click()
    assert update.value.status == 200
    expect(settings.get_by_role("button", name="保存提交窗口")).to_be_enabled()


def choose(page, kind, path):
    with page.expect_file_chooser() as chooser:
        page.locator(f"#upload-{kind}-file").click()
    chooser.value.set_files(path)


def send(page, kind, expected=200):
    with page.expect_response(lambda r: urlsplit(r.url).path.endswith("/api/team/uploads/" + kind)) as result:
        page.locator(f"#upload-{kind}-button").click()
    assert result.value.status == expected
    if expected == 200:
        expect(page.locator(f"#upload-{kind}-status")).to_contain_text("文件已上传，链接已填入当前草稿")
        return page.locator(f"#material-{kind}Url").input_value()


def assert_download(page, link, filename, source):
    with page.expect_download() as received:
        link.click()
    download = received.value
    assert download.suggested_filename == filename
    assert download.path().read_bytes() == source.read_bytes()


def contestant_upload(visit, service, admin):
    project = "演示作品"
    summary = "Synthetic contestant material for the browser acceptance test."
    prefix = service.base_path + "/api/team/uploads/"
    allowed_errors = {(prefix + "files", 415), (prefix + "files", 409)}
    with TemporaryDirectory(prefix="gotc-upload-picker-") as directory, visit("team", allowed_errors=allowed_errors) as page:
        files = Path(directory)
        deck, pdf, archive = files/"slides.pptx", files/"slides.pdf", files/"project.zip"
        empty_presentation(deck)
        print_page = page.context.new_page()
        print_page.set_content("<h1>Synthetic contestant slides</h1>")
        print_page.pdf(path=pdf)
        print_page.close()
        with ZipFile(archive, "w") as bundle:
            bundle.writestr("project/README.md", "Synthetic project\n")
            bundle.writestr("project/main.py", "print('Fixture')\n")

        disabled_upload = page.locator("#upload-slides-button")
        expect(disabled_upload).to_be_disabled()
        expect(disabled_upload).to_have_css("cursor", "not-allowed")
        expect(disabled_upload).not_to_have_attribute("aria-busy", "true")
        expect(page.get_by_text("当前作品提交只读", exact=False)).to_be_visible()
        set_window(admin, True)
        page.reload(wait_until="networkidle")
        for target, selector in (("team-profile", "#team-profile-form"), ("team-score", "#refresh-team-score"), ("team", "#key-form")):
            page.goto(service.origin + service.base_path + "/platform.html#" + target)
            expect(page.locator(selector)).to_be_visible()
        page.locator("#jump-submit-slides").click()
        page.locator("#material-projectName").fill(project)
        page.locator("#material-summary").fill(summary)

        uploaded = {}
        for kind, path in (("slides", deck), ("slides", pdf), ("files", archive)):
            choose(page, kind, path)
            url = send(page, kind)
            expected_name = f"Browser Fixture Team+{project}{path.suffix}"
            link = page.locator(f"#upload-{kind}-result a")
            expect(link).to_have_attribute("href", url)
            assert_download(page, link, expected_name, path)
            expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            with service.db() as db:
                stored = db.execute("SELECT size,sha256 FROM material_uploads WHERE filename=?", (expected_name,)).fetchone()
            assert (stored["size"], stored["sha256"]) == (path.stat().st_size, expected_hash)
            uploaded[kind] = url

        oversized = files / "large.zip"
        with oversized.open("wb") as sparse:
            sparse.truncate(100 * 1024 * 1024 + 1)
        choose(page, "files", oversized)
        page.locator("#upload-files-button").click()
        expect(page.locator("#upload-files-status")).to_contain_text("超过 100 MiB")
        broken = files / "broken.zip"
        broken.write_bytes(b"This is not a ZIP file")
        choose(page, "files", broken)
        send(page, "files", 415)
        expect(page.locator("#upload-files-status")).to_contain_text("文件内容与格式不符")
        choose(page, "files", archive)
        set_window(admin, False)
        send(page, "files", 409)
        expect(page.locator("#upload-files-status")).to_contain_text("已截止")
        expect(page.locator("#material-summary")).to_have_value(summary)
        expect(page.locator("#material-filesUrl")).to_have_value(uploaded["files"])
        set_window(admin, True)
        first_zip = uploaded["files"]
        uploaded["files"] = send(page, "files")
        assert uploaded["files"] != first_zip
        expect(page.locator("#material-filesUrl")).to_have_value(uploaded["files"])

        with page.expect_response(lambda r: urlsplit(r.url).path.endswith("/api/submission")) as draft:
            page.locator("#save-submission-draft").click()
        assert draft.value.status == 200
        version = 1
        expect(page.locator("#submission-receipt h3")).to_have_text(f"草稿编辑版 v{version} 已保存")
        page.reload(wait_until="networkidle")
        expect(page.locator("#material-projectName")).to_have_value(project)
        expect(page.locator("#material-summary")).to_have_value(summary)
        for kind, url in uploaded.items():
            expect(page.locator(f"#material-{kind}Url")).to_have_value(url)
        with visit("judge") as judge:
            expect(judge.locator(".submitted-materials")).to_contain_text("暂未正式提交")
        page.locator("#check-submission").click()
        preview = page.locator("#submission-preview-panel")
        for url in uploaded.values():
            expect(preview.locator(f'a[href="{url}"]')).to_be_visible()
        with page.expect_response(lambda r: urlsplit(r.url).path.endswith("/api/submission")) as formal:
            page.locator("#confirm-submission").click()
        assert formal.value.status == 200
        expect(page.locator("#submission-receipt h3")).to_have_text(f"正式版 v{version + 1} 已收到")
        request_id = page.locator("#submission-receipt .request-number code").inner_text()
        with service.db() as db:
            row = db.execute("SELECT receipt_json FROM submission_requests WHERE request_id=?", (request_id,)).fetchone()
        receipt = json.loads(row["receipt_json"])
        assert (receipt["status"], receipt["action"], receipt["historyVersion"]) == ("succeeded", "submitted", version + 1)
        assert receipt["requestId"] == request_id
        for kind, url in uploaded.items():
            assert receipt["materials"][kind + "Url"] == url
        with visit("judge") as judge:
            formal_links = judge.locator(".submitted-materials")
            expect(formal_links).to_contain_text(summary)
            for kind, path in (("slides", pdf), ("files", archive)):
                assert_download(judge, formal_links.locator(f'a[href="{uploaded[kind]}"]'), f"Browser Fixture Team+{project}{path.suffix}", path)
