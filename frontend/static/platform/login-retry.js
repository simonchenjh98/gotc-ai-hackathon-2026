'use strict';

function platformRequestError(response, body, fallback) {
  const error = new Error(body.error || fallback);
  error.status = response.status;
  error.code = body.code;
  const retryAfter = response.headers?.get('Retry-After');
  if (retryAfter) {
    const date = Date.parse(retryAfter);
    error.retryAfterMs = /^\d+$/.test(retryAfter) ? Number(retryAfter) * 1000
      : Number.isFinite(date) ? Math.max(0, date - Date.now()) : 0;
  }
  return error;
}

// Only the explicit pre-admission refusal is safe to replay. A lost response
// can follow a completed login, claim, or one-time credential operation.
async function withLoginRetry(send, isCurrent, waiting) {
  const retries = 3;
  for (let attempt = 0; ; attempt++) {
    try {
      return await send();
    } catch (error) {
      if (error.status !== 429 || error.code !== 'login_busy') throw error;
      const delay = Math.max(500 * 2 ** attempt, error.retryAfterMs || 0);
      if (attempt >= retries || delay > 5000) {
        error.message = '验证服务仍然繁忙，请稍后手动重试。';
        throw error;
      }
      if (isCurrent()) {
        waiting(`验证服务繁忙，正在等待自动重试（${attempt + 1}/${retries}）…`);
        await new Promise(resolve => setTimeout(resolve, delay + Math.floor(Math.random() * 500)));
      }
      if (!isCurrent()) throw new Error('输入或登录身份已变化，请核对当前信息后重试。');
    }
  }
}
