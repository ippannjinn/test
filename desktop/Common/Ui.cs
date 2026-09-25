using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Linq;
using System.Windows.Forms;

namespace NextAI.Common
{
    public static class Ui
    {
        public static readonly Color Accent = Color.FromArgb(79, 70, 229);
        public static readonly Color Ok = Color.FromArgb(22, 163, 74);
        public static readonly Color Warn = Color.FromArgb(217, 119, 6);
        public static readonly Color Bad = Color.FromArgb(220, 38, 38);
        public static readonly Color Muted = Color.FromArgb(107, 114, 136);
        public static readonly Color PanelBg = Color.FromArgb(246, 247, 251);
        public static Font BaseFont = new Font("Yu Gothic UI", 9.5f);
        public static Font BoldFont = new Font("Yu Gothic UI", 9.5f, FontStyle.Bold);
        public static Font BigFont = new Font("Yu Gothic UI", 16f, FontStyle.Bold);
        public static Font MonoFont = new Font("Consolas", 9.5f);

        public static Icon AppIcon()
        {
            try { return Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { return SystemIcons.Application; }
        }

        public static Button Btn(string text, EventHandler onClick, bool primary = false)
        {
            var b = new Button { Text = text, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Padding = new Padding(8, 2, 8, 2), Margin = new Padding(3), UseVisualStyleBackColor = !primary };
            if (primary) { b.BackColor = Accent; b.ForeColor = Color.White; b.FlatStyle = FlatStyle.Flat; b.FlatAppearance.BorderSize = 0; }
            if (onClick != null) b.Click += onClick;
            return b;
        }

        public static FlowLayoutPanel Toolbar(params Control[] controls)
        {
            var p = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Padding = new Padding(4), WrapContents = true };
            p.Controls.AddRange(controls);
            return p;
        }

        public static Label Label(string text, Font font = null, Color? color = null) =>
            new Label { Text = text, AutoSize = true, Font = font ?? BaseFont, ForeColor = color ?? SystemColors.ControlText, Margin = new Padding(3, 7, 3, 3) };

        public static DataGridView Grid(params (string key, string header, int width)[] cols)
        {
            var g = new DataGridView
            {
                Dock = DockStyle.Fill, ReadOnly = true, AllowUserToAddRows = false, AllowUserToDeleteRows = false, AllowUserToResizeRows = false,
                SelectionMode = DataGridViewSelectionMode.FullRowSelect, MultiSelect = false, RowHeadersVisible = false,
                BackgroundColor = Color.White, BorderStyle = BorderStyle.None, AutoSizeRowsMode = DataGridViewAutoSizeRowsMode.None,
                ColumnHeadersHeightSizeMode = DataGridViewColumnHeadersHeightSizeMode.AutoSize,
            };
            g.DefaultCellStyle.SelectionBackColor = Color.FromArgb(224, 231, 255);
            g.DefaultCellStyle.SelectionForeColor = Color.Black;
            g.AlternatingRowsDefaultCellStyle.BackColor = Color.FromArgb(249, 250, 253);
            foreach (var (key, header, width) in cols)
            {
                var c = new DataGridViewTextBoxColumn { Name = key, HeaderText = header, SortMode = DataGridViewColumnSortMode.Automatic };
                if (width > 0) c.Width = width;
                else { c.MinimumWidth = 120; c.AutoSizeMode = DataGridViewAutoSizeColumnMode.Fill; }
                g.Columns.Add(c);
            }
            return g;
        }

        public static void Fill(DataGridView g, IEnumerable<JObject> rows, Func<JObject, object[]> map, Action<DataGridViewRow, JObject> style = null)
        {
            var selected = g.CurrentRow?.Tag is JObject cur ? cur.Str("id", cur.Str("key", cur.Str("name"))) : null;
            var firstRow = g.FirstDisplayedScrollingRowIndex;
            g.SuspendLayout();
            g.Rows.Clear();
            foreach (var r in rows)
            {
                var idx = g.Rows.Add(map(r));
                var row = g.Rows[idx];
                row.Tag = r;
                style?.Invoke(row, r);
                if (selected != null && r.Str("id", r.Str("key", r.Str("name"))) == selected) { row.Selected = true; g.CurrentCell = row.Cells[0]; }
            }
            if (firstRow >= 0 && firstRow < g.Rows.Count) g.FirstDisplayedScrollingRowIndex = firstRow;
            if (selected == null) g.ClearSelection();
            g.ResumeLayout();
        }

        public static JObject Selected(DataGridView g) => g.CurrentRow?.Tag as JObject;

        public static Color StatusColor(string status)
        {
            switch (status)
            {
                case "PASS": case "ok": case "active": case "Running": case "hot": case "NORMAL": return Ok;
                case "WARN": case "ELEVATED": case "suspended": case "loading": case "warm": return Warn;
                case "FAIL": case "CRITICAL": case "HIGH": case "error": case "disabled": return Bad;
                default: return Muted;
            }
        }

        public static string Bytes(double b)
        {
            if (b < 1024) return $"{b:F0} B";
            if (b < 1048576) return $"{b / 1024:F1} KB";
            if (b < 1073741824) return $"{b / 1048576:F1} MB";
            return $"{b / 1073741824:F2} GB";
        }

        public static string Time(double ts) => ts <= 0 ? "-" : DateTimeOffset.FromUnixTimeMilliseconds((long)(ts * 1000)).ToLocalTime().ToString("MM/dd HH:mm");

        public static string Duration(double seconds)
        {
            var t = TimeSpan.FromSeconds(Math.Max(0, seconds));
            return t.TotalHours >= 1 ? $"{(int)t.TotalHours}時間{t.Minutes}分" : t.TotalMinutes >= 1 ? $"{t.Minutes}分{t.Seconds}秒" : $"{t.Seconds}秒";
        }

        public static void Error(IWin32Window owner, Exception ex) =>
            MessageBox.Show(owner, ex is ApiException ? ex.Message : ex.ToString(), "エラー", MessageBoxButtons.OK, MessageBoxIcon.Error);

        public static bool Confirm(IWin32Window owner, string text, string title = "確認") =>
            MessageBox.Show(owner, text, title, MessageBoxButtons.OKCancel, MessageBoxIcon.Warning, MessageBoxDefaultButton.Button2) == DialogResult.OK;

        public static string Prompt(IWin32Window owner, string title, string label, string initial = "", bool password = false)
        {
            using (var f = new Form { Text = title, FormBorderStyle = FormBorderStyle.FixedDialog, StartPosition = FormStartPosition.CenterParent, MinimizeBox = false, MaximizeBox = false, ClientSize = new Size(420, 130), Font = BaseFont, AutoScaleMode = AutoScaleMode.Dpi })
            {
                var l = new Label { Text = label, Left = 12, Top = 12, Width = 396, Height = 36 };
                var t = new TextBox { Left = 12, Top = 50, Width = 396, Text = initial, UseSystemPasswordChar = password };
                var ok = new Button { Text = "OK", DialogResult = DialogResult.OK, Left = 236, Top = 88, Width = 80 };
                var cancel = new Button { Text = "キャンセル", DialogResult = DialogResult.Cancel, Left = 328, Top = 88, Width = 80 };
                f.Controls.AddRange(new Control[] { l, t, ok, cancel });
                f.AcceptButton = ok;
                f.CancelButton = cancel;
                return f.ShowDialog(owner) == DialogResult.OK ? t.Text : null;
            }
        }

        public static void ShowText(IWin32Window owner, string title, string text, string hint = "")
        {
            using (var f = new Form { Text = title, StartPosition = FormStartPosition.CenterParent, ClientSize = new Size(560, 380), Font = BaseFont, MinimizeBox = false, AutoScaleMode = AutoScaleMode.Dpi })
            {
                var box = new TextBox { Multiline = true, ReadOnly = true, TabStop = false, Dock = DockStyle.Fill, Text = text.Replace("\n", "\r\n"), ScrollBars = ScrollBars.Vertical, Font = MonoFont, BackColor = Color.White };
                var bar = new FlowLayoutPanel { Dock = DockStyle.Bottom, FlowDirection = FlowDirection.RightToLeft, AutoSize = true, Padding = new Padding(6) };
                var close = new Button { Text = "閉じる", DialogResult = DialogResult.OK, AutoSize = true };
                var copy = new Button { Text = "クリップボードにコピー", AutoSize = true };
                copy.Click += (s, e) => { Clipboard.SetText(text); copy.Text = "コピーしました"; };
                bar.Controls.AddRange(new Control[] { close, copy });
                if (!string.IsNullOrEmpty(hint)) bar.Controls.Add(new Label { Text = hint, AutoSize = true, ForeColor = Muted, Margin = new Padding(3, 8, 20, 3) });
                f.Controls.Add(box);
                f.Controls.Add(bar);
                f.AcceptButton = close;
                f.Shown += (s, e) => { box.SelectionLength = 0; close.Focus(); };
                f.ShowDialog(owner);
            }
        }
    }

    /// <summary>Dashboard tile: title, big value, sub text and an optional utilisation bar.</summary>
    public sealed class MetricTile : Control
    {
        public string Title = "", Value = "-", Sub = "";
        public double? Ratio;
        public Color BarColor = Ui.Accent;

        public MetricTile()
        {
            DoubleBuffered = true;
            Size = new Size(210, 92);
            Margin = new Padding(6);
            BackColor = Color.White;
            SetStyle(ControlStyles.ResizeRedraw, true);
        }

        public void Set(string value, string sub = "", double? ratio = null, Color? color = null)
        {
            Value = value; Sub = sub; Ratio = ratio;
            BarColor = color ?? (ratio >= 0.9 ? Ui.Bad : ratio >= 0.75 ? Ui.Warn : Ui.Accent);
            Invalidate();
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;
            using (var border = new Pen(Color.FromArgb(226, 229, 239)))
                g.DrawRectangle(border, 0, 0, Width - 1, Height - 1);
            var pad = 10;
            TextRenderer.DrawText(g, Title, Ui.BaseFont, new Point(pad, 6), Ui.Muted);
            TextRenderer.DrawText(g, Value, Ui.BigFont, new Rectangle(pad, 24, Width - 2 * pad, 32), ForeColor, TextFormatFlags.EndEllipsis | TextFormatFlags.Left);
            TextRenderer.DrawText(g, Sub, Ui.BaseFont, new Rectangle(pad, 56, Width - 2 * pad, 18), Ui.Muted, TextFormatFlags.EndEllipsis | TextFormatFlags.Left);
            if (Ratio.HasValue)
            {
                var r = new Rectangle(pad, Height - 12, Width - 2 * pad, 5);
                using (var bg = new SolidBrush(Color.FromArgb(236, 238, 245))) g.FillRectangle(bg, r);
                using (var fg = new SolidBrush(BarColor)) g.FillRectangle(fg, new Rectangle(r.X, r.Y, (int)(r.Width * Math.Max(0, Math.Min(1, Ratio.Value))), r.Height));
            }
        }
    }

    /// <summary>Minimal line chart for recent metric history.</summary>
    public sealed class Sparkline : Control
    {
        public string Title = "";
        public string Unit = "";
        public double Max = 100;
        public List<double> Values = new List<double>();
        public Color LineColor = Ui.Accent;

        public Sparkline()
        {
            DoubleBuffered = true;
            Size = new Size(320, 120);
            Margin = new Padding(6);
            BackColor = Color.White;
            SetStyle(ControlStyles.ResizeRedraw, true);
        }

        public void SetValues(IEnumerable<double> values, double max)
        {
            Values = values.ToList();
            Max = max > 0 ? max : 1;
            Invalidate();
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            using (var border = new Pen(Color.FromArgb(226, 229, 239))) g.DrawRectangle(border, 0, 0, Width - 1, Height - 1);
            var last = Values.Count > 0 ? Values[Values.Count - 1] : 0;
            TextRenderer.DrawText(g, $"{Title}  {last:F0}{Unit}", Ui.BaseFont, new Point(8, 5), Ui.Muted);
            var plot = new Rectangle(8, 26, Width - 16, Height - 34);
            using (var grid = new Pen(Color.FromArgb(240, 242, 247)))
                for (var i = 0; i <= 4; i++) g.DrawLine(grid, plot.Left, plot.Top + plot.Height * i / 4, plot.Right, plot.Top + plot.Height * i / 4);
            if (Values.Count < 2) return;
            var pts = Values.Select((v, i) => new PointF(plot.Left + plot.Width * i / (float)(Values.Count - 1),
                plot.Bottom - (float)(plot.Height * Math.Max(0, Math.Min(1, v / Max))))).ToArray();
            using (var fill = new GraphicsPath())
            {
                fill.AddLines(pts);
                fill.AddLine(pts[pts.Length - 1], new PointF(plot.Right, plot.Bottom));
                fill.AddLine(new PointF(plot.Right, plot.Bottom), new PointF(plot.Left, plot.Bottom));
                using (var b = new SolidBrush(Color.FromArgb(28, LineColor))) g.FillPath(b, fill);
            }
            using (var pen = new Pen(LineColor, 2f)) g.DrawLines(pen, pts);
        }
    }
}
