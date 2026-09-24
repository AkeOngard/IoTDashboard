/* Login form (templates/login.html). A file rather than an inline <script>,
 * so the CSP can refuse inline scripts outright. */
function login() {
  return {
    password: '', busy: false, error: '',

    async submit() {
      this.busy = true; this.error = '';
      try {
        await apiFetch('/api/auth/login', { json: { password: this.password } });
        // The password was right, but that does not prove the browser kept
        // the cookie. If it refused it, navigating on would bounce straight
        // back here with no explanation -- so ask before leaving.
        const status = await apiFetch('/api/auth/status');
        if (!status.authenticated) {
          this.error = 'รหัสผ่านถูกต้อง แต่เบราว์เซอร์ไม่ยอมเก็บ cookie — '
                     + 'ลองเปิดผ่านที่อยู่ https แทน http หรือเปิดการยอมรับ cookie';
          this.password = '';
          this.busy = false;
          return;
        }
        // Full navigation rather than a fetch: the dashboard opens its
        // WebSocket on load, and it needs the cookie that is now in place.
        window.location.replace('/');
      } catch (err) {
        // The API and the logs speak English on purpose; this screen is the
        // one a person reads while standing in their hallway.
        this.error = {
          401: 'รหัสผ่านไม่ถูกต้อง',
          429: err.message,          // already says how long to wait
          503: 'ยังไม่ได้ตั้งรหัสผ่านบนเครื่องที่รัน dashboard',
          0:   'ติดต่อเซิร์ฟเวอร์ไม่ได้',
        }[err.status] || err.message || 'เข้าสู่ระบบไม่สำเร็จ';
        this.password = '';
        this.busy = false;
      }
    },
  };
}
