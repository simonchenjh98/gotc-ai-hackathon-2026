"""Check the shipped button states with actual pointer and keyboard input."""
from playwright.sync_api import expect


def button_states(page, base):
    page.goto(base)
    tab = page.locator('#tab-day2')
    expect(tab).to_have_attribute('aria-selected', 'false')
    resting = tab.evaluate('el => getComputedStyle(el).boxShadow')
    tab.hover()
    assert tab.evaluate('el => getComputedStyle(el).boxShadow') != resting
    page.mouse.down()
    expect(tab).to_have_css('translate', '0px 1px')
    page.mouse.up()
    expect(tab).to_have_attribute('aria-selected', 'true')
    tab.press('ArrowLeft')
    first = page.locator('#tab-day1')
    expect(first).to_be_focused()
    expect(first).to_have_attribute('aria-selected', 'true')
    assert first.evaluate("el => el.matches(':focus-visible') && getComputedStyle(el).outlineWidth !== '0px'")

    page.goto(base + 'teams.html')
    refresh = page.locator('[data-reg-refresh]')
    expect(refresh).to_be_enabled()
    pending = []
    page.route('**/api/registration', lambda route: pending.append(route))
    with page.expect_request('**/api/registration'):
        refresh.press('Enter')
    expect(refresh).to_have_attribute('aria-busy', 'true')
    expect(refresh).to_have_css('cursor', 'progress')
    assert refresh.evaluate("el => getComputedStyle(el, '::after').animationName") == 'gotc-button-spin'
    page.emulate_media(reduced_motion='reduce')
    assert refresh.evaluate("el => getComputedStyle(el, '::after').animationName") == 'none'
    pending.pop().continue_()
    expect(refresh).to_be_enabled()
    expect(refresh).not_to_have_attribute('aria-busy', 'true')
