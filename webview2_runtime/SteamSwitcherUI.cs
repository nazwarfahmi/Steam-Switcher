using System;
using System.Drawing;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace SteamSwitcherHost
{
    static class NativeMethods
    {
        public const int GWL_EXSTYLE = -20;
        public const int WS_EX_LAYERED = 0x00080000;
        public const int WS_EX_TRANSPARENT = 0x00000020;
        public const int WS_EX_TOOLWINDOW = 0x00000080;
        public const int WS_EX_NOACTIVATE = 0x08000000;
        public const int WS_EX_TOPMOST = 0x00000008;

        public static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
        public const uint SWP_NOACTIVATE = 0x0010;
        public const uint SWP_SHOWWINDOW = 0x0040;

        [DllImport("user32.dll")]
        public static extern int GetWindowLong(IntPtr hWnd, int nIndex);

        [DllImport("user32.dll")]
        public static extern int SetWindowLong(IntPtr hWnd, int nIndex, int dwNewLong);

        [DllImport("user32.dll")]
        public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);

        [DllImport("user32.dll")]
        public static extern bool SetForegroundWindow(IntPtr hWnd);

        [DllImport("dwmapi.dll")]
        public static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int attrValue, int attrSize);
    }

    public class OverlayForm : Form
    {
        private WebView2 webView;
        private const int OW = 392;
        private const int OH = 116;

        public OverlayForm()
        {
            this.FormBorderStyle = FormBorderStyle.None;
            this.ShowInTaskbar = false;
            this.TopMost = true;
            this.StartPosition = FormStartPosition.Manual;
            this.Size = new Size(OW, OH);
            this.Location = new Point(-30000, -30000);
            this.BackColor = Color.FromArgb(15, 23, 42);

            webView = new WebView2();
            webView.Dock = DockStyle.Fill;
            webView.DefaultBackgroundColor = Color.Transparent;
            this.Controls.Add(webView);
        }

        protected override bool ShowWithoutActivation
        {
            get { return true; }
        }

        protected override CreateParams CreateParams
        {
            get
            {
                CreateParams cp = base.CreateParams;
                cp.ExStyle |= NativeMethods.WS_EX_TOOLWINDOW | NativeMethods.WS_EX_NOACTIVATE | NativeMethods.WS_EX_TOPMOST | NativeMethods.WS_EX_LAYERED | NativeMethods.WS_EX_TRANSPARENT;
                return cp;
            }
        }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            int ex = NativeMethods.GetWindowLong(this.Handle, NativeMethods.GWL_EXSTYLE);
            NativeMethods.SetWindowLong(this.Handle, NativeMethods.GWL_EXSTYLE, ex | NativeMethods.WS_EX_LAYERED | NativeMethods.WS_EX_TRANSPARENT | NativeMethods.WS_EX_NOACTIVATE | NativeMethods.WS_EX_TOOLWINDOW);
        }

        public async Task InitializeAsync(CoreWebView2Environment env, string baseUrl)
        {
            await webView.EnsureCoreWebView2Async(env);
            webView.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
            webView.CoreWebView2.Settings.AreDevToolsEnabled = false;
            webView.CoreWebView2.Settings.IsStatusBarEnabled = false;
            webView.CoreWebView2.DocumentTitleChanged += (s, ev) =>
            {
                string title = webView.CoreWebView2.DocumentTitle ?? "";
                if (title == "SHOW_OVERLAY")
                {
                    Rectangle wa = Screen.PrimaryScreen.WorkingArea;
                    int tx = wa.Right - OW - 8;
                    int ty = wa.Bottom - OH - 8;
                    NativeMethods.SetWindowPos(this.Handle, NativeMethods.HWND_TOPMOST, tx, ty, OW, OH, NativeMethods.SWP_NOACTIVATE | NativeMethods.SWP_SHOWWINDOW);
                }
                else if (title == "HIDE_OVERLAY")
                {
                    NativeMethods.SetWindowPos(this.Handle, NativeMethods.HWND_TOPMOST, -30000, -30000, OW, OH, NativeMethods.SWP_NOACTIVATE);
                }
            };
            webView.CoreWebView2.Navigate(baseUrl + "/overlay.html");
        }
    }

    public class MainForm : Form
    {
        private WebView2 webView;
        private OverlayForm overlayForm;
        private string baseUrl;
        private bool startHidden;
        private EventWaitHandle showEvent;
        public bool ForceExit = false;

        public MainForm(string url, int w, int h, int? x, int? y, bool hidden, EventWaitHandle evt)
        {
            this.baseUrl = url;
            this.startHidden = hidden;
            this.showEvent = evt;

            this.Text = "Steam Switcher";
            this.MinimumSize = new Size(680, 480);
            this.Size = new Size(Math.Max(680, w), Math.Max(480, h));
            this.BackColor = Color.FromArgb(11, 15, 25);

            if (x.HasValue && y.HasValue)
            {
                this.StartPosition = FormStartPosition.Manual;
                this.Location = new Point(x.Value, y.Value);
            }
            else
            {
                this.StartPosition = FormStartPosition.CenterScreen;
            }

            string icoPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "logo.ico");
            if (File.Exists(icoPath))
            {
                try { this.Icon = new Icon(icoPath); } catch { }
            }

            webView = new WebView2();
            webView.Dock = DockStyle.Fill;
            webView.DefaultBackgroundColor = Color.FromArgb(255, 11, 15, 25);
            this.Controls.Add(webView);

            this.Load += MainForm_Load;
            this.FormClosing += MainForm_FormClosing;

            Thread listenerThread = new Thread(ListenForShowSignal);
            listenerThread.IsBackground = true;
            listenerThread.Start();
        }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            try
            {
                int dark = 1;
                NativeMethods.DwmSetWindowAttribute(this.Handle, 20, ref dark, sizeof(int));
            }
            catch { }
        }

        private void ListenForShowSignal()
        {
            while (!ForceExit)
            {
                try
                {
                    if (showEvent.WaitOne(500))
                    {
                        if (ForceExit) break;
                        this.BeginInvoke(new Action(() =>
                        {
                            this.Show();
                            if (this.WindowState == FormWindowState.Minimized)
                                this.WindowState = FormWindowState.Normal;
                            this.Activate();
                            NativeMethods.SetForegroundWindow(this.Handle);
                        }));
                    }
                }
                catch { break; }
            }
        }

        private async void MainForm_Load(object sender, EventArgs e)
        {
            if (startHidden)
            {
                this.Hide();
            }

            string userDataDir = Path.Combine(Path.GetTempPath(), "SteamSwitcher_WebView2Profile");
            var opts = new CoreWebView2EnvironmentOptions("--autoplay-policy=no-user-gesture-required --disable-features=Translate --disable-extensions --no-proxy-server");
            var env = await CoreWebView2Environment.CreateAsync(null, userDataDir, opts);

            await webView.EnsureCoreWebView2Async(env);
            webView.CoreWebView2.Settings.IsStatusBarEnabled = false;
            webView.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
            webView.CoreWebView2.Navigate(baseUrl);

            overlayForm = new OverlayForm();
            overlayForm.Show();
            await overlayForm.InitializeAsync(env, baseUrl);
        }

        private void MainForm_FormClosing(object sender, FormClosingEventArgs e)
        {
            if (!ForceExit && e.CloseReason == CloseReason.UserClosing)
            {
                e.Cancel = true;
                Rectangle b = (this.WindowState == FormWindowState.Normal) ? this.Bounds : this.RestoreBounds;
                this.Hide();
                ThreadPool.QueueUserWorkItem(_ =>
                {
                    try
                    {
                        var req = (HttpWebRequest)WebRequest.Create(baseUrl + "/api/window_closed");
                        req.Method = "POST";
                        req.ContentType = "application/json";
                        req.Timeout = 1500;
                        string json = string.Format("{{\"width\":{0},\"height\":{1},\"x\":{2},\"y\":{3}}}", b.Width, b.Height, b.X, b.Y);
                        byte[] data = Encoding.UTF8.GetBytes(json);
                        req.ContentLength = data.Length;
                        using (var s = req.GetRequestStream())
                        {
                            s.Write(data, 0, data.Length);
                        }
                        using (var resp = req.GetResponse()) { }
                    }
                    catch { }
                });
            }
            else
            {
                if (overlayForm != null && !overlayForm.IsDisposed)
                {
                    try { overlayForm.Close(); } catch { }
                }
            }
        }
    }

    static class Program
    {
        [DllImport("user32.dll")]
        private static extern bool SetProcessDPIAware();

        [STAThread]
        static void Main(string[] args)
        {
            try { SetProcessDPIAware(); } catch { }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            bool createdNew;
            EventWaitHandle showEvt = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\SteamSwitcher_WV2_ShowEvent", out createdNew);
            if (!createdNew)
            {
                // Wake up existing WebView2 UI instance immediately
                showEvt.Set();
                return;
            }

            string baseUrl = "http://127.0.0.1:18420";
            int w = 1280, h = 820;
            int? x = null, y = null;
            bool hidden = false;

            foreach (string a in args)
            {
                if (a.StartsWith("--app-url=")) baseUrl = a.Substring("--app-url=".Length);
                else if (a.StartsWith("--win-w=")) int.TryParse(a.Substring("--win-w=".Length), out w);
                else if (a.StartsWith("--win-h=")) int.TryParse(a.Substring("--win-h=".Length), out h);
                else if (a.StartsWith("--win-x=")) { int vx; if (int.TryParse(a.Substring("--win-x=".Length), out vx)) x = vx; }
                else if (a.StartsWith("--win-y=")) { int vy; if (int.TryParse(a.Substring("--win-y=".Length), out vy)) y = vy; }
                else if (a == "--hidden") hidden = true;
            }

            MainForm mainForm = new MainForm(baseUrl, w, h, x, y, hidden, showEvt);
            Application.Run(mainForm);
        }
    }
}
