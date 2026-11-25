'use strict';

// Keep eligibility (disabled) under the feature's control. Only the action that
// started a pending operation gets its loading indicator.
function buttonAction(handler) {
  return function handleButtonAction(event) {
    const control = event?.submitter || event?.target?.closest?.('button, [role="button"], input[type="submit"], a[href]') ||
      (this?.matches?.('button, [role="button"]') ? this : null);
    if (control?.getAttribute?.('aria-busy') === 'true') {
      event?.preventDefault();
      return;
    }
    const result = handler.apply(this, arguments);
    if (!control || typeof result?.then !== 'function') return result;
    control.setAttribute('aria-busy', 'true');
    return result.finally(() => control.removeAttribute('aria-busy'));
  };
}
