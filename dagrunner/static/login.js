const form = document.getElementById("loginForm");
const username = document.getElementById("username");
const password = document.getElementById("password");
const button = document.getElementById("loginButton");
const errorBox = document.getElementById("loginError");
let countdownTimer = null;

async function sha256(value) {
  if (!window.crypto?.subtle) {
    throw new Error("当前页面无法安全计算密码摘要，请使用 HTTPS 或在本机访问");
  }
  const bytes = new TextEncoder().encode(value);
  const digest = await window.crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("");
}

function showCountdown(seconds) {
  clearInterval(countdownTimer);
  let remaining = Math.max(1, Number(seconds) || 600);
  const render = () => {
    const minutes = Math.floor(remaining / 60);
    const secs = String(remaining % 60).padStart(2, "0");
    errorBox.textContent = `此 IP 已暂时禁止登录，请在 ${minutes}:${secs} 后重试`;
    button.disabled = true;
    if (remaining <= 0) {
      clearInterval(countdownTimer);
      countdownTimer = null;
      errorBox.textContent = "可以重新尝试登录";
      button.disabled = false;
      return;
    }
    remaining -= 1;
  };
  render();
  countdownTimer = setInterval(render, 1000);
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  button.disabled = true;
  button.textContent = "正在验证…";
  errorBox.textContent = "";
  try {
    const passwordHash = await sha256(password.value);
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: username.value.trim(),
        password_hash: passwordHash,
        next: document.body.dataset.nextUrl || "/",
      }),
    });
    const result = await response.json();
    if (response.status === 429) {
      showCountdown(result.retry_after);
      return;
    }
    if (!response.ok) {
      const suffix = Number.isInteger(result.remaining_attempts)
        ? `，还可尝试 ${result.remaining_attempts} 次`
        : "";
      throw new Error(`${result.error || "登录失败"}${suffix}`);
    }
    window.location.replace(result.redirect || "/");
  } catch (error) {
    errorBox.textContent = error.message || "登录失败";
  } finally {
    password.value = "";
    if (!countdownTimer) button.disabled = false;
    button.textContent = "登录";
  }
});
