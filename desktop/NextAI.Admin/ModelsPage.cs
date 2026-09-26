using System;
using System.Drawing;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using System.Windows.Forms;
using NextAI.Common;

namespace NextAI.Admin
{
    sealed class ModelsPage : AdminPage
    {
        readonly DataGridView grid;
        readonly ListBox swapList;
        readonly Label backends;
        public override bool AutoRefresh => true;

        public ModelsPage(MainForm main) : base(main, "モデル")
        {
            grid = Ui.Grid(("name", "モデル", 0), ("kind", "種別", 70), ("roles", "役割", 110), ("size", "サイズ", 70), ("installed", "導入", 50),
                ("enabled", "有効", 50), ("state", "状態", 80), ("inuse", "使用中", 55), ("vram", "VRAM予測", 80), ("plan", "配置 (Hot/Warm/Cold)", 230),
                ("load", "ロード時間", 75), ("dl", "ダウンロード", 130), ("license", "ライセンス", 120));
            var bar = Ui.Toolbar(
                Ui.Btn("ロード", (s, e) => Act("load")), Ui.Btn("アンロード", (s, e) => Act("unload")),
                Ui.Btn("有効化", (s, e) => Act("enable")), Ui.Btn("無効化", (s, e) => Act("disable")),
                Ui.Btn("ダウンロード / 再開", (s, e) => Act("download")), Ui.Btn("ダウンロード中止", (s, e) => Act("download/cancel")),
                Ui.Btn("ファイル削除", (s, e) => DeleteFiles()), Ui.Btn("カスタムモデル追加", (s, e) => AddCustom()),
                Ui.Btn("推奨モデルセット", (s, e) => ShowSets()), Ui.Btn("更新", (s, e) => Run(() => Task.CompletedTask)));
            backends = Ui.Label("", null, Ui.Muted);
            backends.Dock = DockStyle.Top;
            swapList = new ListBox { IntegralHeight = false, BorderStyle = BorderStyle.None };
            var split = new SplitContainer { Dock = DockStyle.Fill, Orientation = Orientation.Horizontal };
            split.Panel1.Controls.Add(grid);
            split.Panel2.Controls.Add(Group("モデルスワップ履歴 (スラッシング検出時は自動でプリフェッチを抑制)", swapList));
            Controls.Add(split);
            Controls.Add(backends);
            Controls.Add(bar);
            Layout += (s, e) => { if (split.Height > 300) split.SplitterDistance = split.Height - 170; };
        }

        public override async Task RefreshAsync()
        {
            var d = await Api.GetAsync("/api/admin/models");
            Ui.Fill(grid, d.Arr("models").Objects(), m =>
            {
                var plan = m.Obj("plan");
                var dl = m.Obj("download");
                var dlText = "";
                if (dl.Count > 0)
                {
                    var st = dl.Str("state");
                    dlText = st == "running" ? $"{Ui.Bytes(dl.Num("done"))} / {Ui.Bytes(dl.Num("total"))}" : st == "done" ? "完了" : "失敗: " + dl.Str("error");
                }
                var cal = m.Obj("calibration");
                return new object[]
                {
                    m.Str("display_name"), m.Str("kind"), string.Join(",", m.Arr("roles").Cast<object>()), $"{m.Num("size_gb"):F1}GB",
                    m.Bool("installed") ? "✓" : "", m.Bool("enabled") ? "✓" : "", StateText(m.Str("state")), m.Int("in_use"),
                    plan.Count > 0 ? $"{plan.Int("est_vram_mb")}MB" : "", plan.Count > 0 ? string.Join(" ", plan.Arr("notes").Cast<object>()) + (plan.Int("ctx") > 0 ? $" ctx={plan.Int("ctx")}" : "") : m.Str("error"),
                    cal.Count > 0 && cal.Num("load_seconds") > 0 ? $"{cal.Num("load_seconds"):F1}s (実測)" : $"{m.Num("expected_load_seconds"):F0}s (推定)",
                    dlText, m.Str("license"),
                };
            }, (row, m) => row.Cells["state"].Style.ForeColor = Ui.StatusColor(m.Str("state")));
            var b = d.Obj("backends");
            backends.Text = $"バックエンド: {b.Str("mode")}  |  LLM: {Avail(b.Obj("llm"))}  画像: {Avail(b.Obj("image"))}  動画: {Avail(b.Obj("video"))}  音楽: {Avail(b.Obj("music"))}"
                            + (d.Bool("thrashing") ? "  |  ⚠ スラッシング検出中" : "");
            swapList.BeginUpdate();
            swapList.Items.Clear();
            foreach (var s in d.Arr("swaps").Objects())
                swapList.Items.Add($"{Ui.Time(s.Num("ts"))}  {s.Str("loaded")} ({s.Num("seconds"):F1}s)  理由: {s.Str("reason")}  退避: {string.Join(",", s.Arr("evicted").Cast<object>())}");
            swapList.EndUpdate();
        }

        static string Avail(JObject o) => $"{o.Str("name")}{(o.Bool("available") ? "" : "(未導入)")}";

        static string StateText(string s) => s == "hot" ? "Hot (VRAM)" : s == "warm" ? "Warm (RAM)" : s == "cold" ? "Cold (NVMe)" : s == "loading" ? "ロード中" : s == "unloading" ? "解放中" : s == "error" ? "エラー" : s;

        void Act(string action)
        {
            var m = RequireSelection(grid, "モデル");
            if (m == null) return;
            if (action == "download" && !Ui.Confirm(this, $"{m.Str("display_name")} (約{m.Num("size_gb"):F1}GB) をダウンロードします。\n中断しても続きから再開できます。", "ダウンロード")) return;
            Run(() => Api.PostAsync($"/api/admin/models/{m.Str("id")}/{action}"));
        }

        void DeleteFiles()
        {
            var m = RequireSelection(grid, "モデル");
            if (m == null) return;
            var typed = Ui.Prompt(this, "モデルファイル削除", $"{m.Str("display_name")} のファイルを削除します (再ダウンロードで復元可能)。\n確認のためモデルIDを入力: {m.Str("id")}");
            if (typed == null) return;
            Run(() => Api.DeleteAsync($"/api/admin/models/{m.Str("id")}/files", new JObject { ["confirm"] = typed }));
        }

        void ShowSets()
        {
            Run(async () =>
            {
                var d = await Api.GetAsync("/api/admin/model-sets");
                var lines = d.Arr("sets").Objects().Select(s =>
                    $"{(s.Str("id") == d.Str("selected") ? "★ " : "   ")}{s.Str("name")} [{s.Str("id")}]  約{s.Num("size_gb"):F0}GB  {(s.Bool("eligible") ? "このPCで運用可" : "要件未達")}\n      {string.Join(", ", s.Arr("models").Cast<object>())}");
                Ui.ShowText(this, "モデルセット", "このPCの VRAM / RAM / 空き容量から、長期運用できる構成を選んでいます (★=推奨)。\n\n" + string.Join("\n\n", lines));
            }, false);
        }

        void AddCustom()
        {
            using (var dlg = new CustomModelDialog())
            {
                if (dlg.ShowDialog(this) != DialogResult.OK) return;
                Run(() => Api.PostAsync("/api/admin/models/custom", dlg.Result));
            }
        }
    }

    sealed class CustomModelDialog : Form
    {
        public JObject Result;

        public CustomModelDialog()
        {
            Text = "カスタムモデル追加 (Hugging Face GGUF)";
            Font = Ui.BaseFont;
            AutoScaleMode = AutoScaleMode.Dpi;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = MinimizeBox = false;
            StartPosition = FormStartPosition.CenterParent;
            ClientSize = new Size(520, 470);
            var t = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, Padding = new Padding(12) };
            t.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 170));
            t.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            TextBox Tb(string v = "") => new TextBox { Dock = DockStyle.Fill, Text = v };
            var id = Tb(); var name = Tb(); var repo = Tb("org/Model-GGUF"); var pattern = Tb("*Q4_K_M.gguf"); var mmproj = Tb();
            var kind = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Dock = DockStyle.Fill };
            kind.Items.AddRange(new object[] { "llm", "vlm", "embedding" });
            kind.SelectedIndex = 0;
            var roles = Tb("chat");
            var size = Tb("5.0"); var layers = Tb("32"); var kv = Tb("8"); var head = Tb("128"); var ctx = Tb("16384");
            var moe = new CheckBox { Text = "MoE (エキスパートをRAM/NVMeへオフロード可能)", AutoSize = true };
            var license = Tb("unknown");
            void Row(string l, Control c) { t.Controls.Add(new Label { Text = l, AutoSize = true, Margin = new Padding(3, 7, 3, 3) }); t.Controls.Add(c); }
            Row("ID (英小文字)", id); Row("表示名", name); Row("種別", kind); Row("HFリポジトリ", repo); Row("ファイルパターン", pattern);
            Row("mmprojパターン (VLM)", mmproj); Row("役割 (fast,general,coding…)", roles); Row("サイズ (GB)", size); Row("レイヤー数", layers);
            Row("KVヘッド数", kv); Row("ヘッド次元", head); Row("最大コンテキスト", ctx); Row("", moe); Row("ライセンス", license);
            var ok = new Button { Text = "追加", DialogResult = DialogResult.OK, AutoSize = true };
            var cancel = new Button { Text = "キャンセル", DialogResult = DialogResult.Cancel, AutoSize = true };
            var bar = new FlowLayoutPanel { Dock = DockStyle.Bottom, FlowDirection = FlowDirection.RightToLeft, AutoSize = true, Padding = new Padding(8) };
            bar.Controls.AddRange(new Control[] { cancel, ok });
            Controls.Add(t);
            Controls.Add(bar);
            ok.Click += (s, e) =>
            {
                double sz; int ly, kh, hd, cx;
                if (!double.TryParse(size.Text, NumberStyles.Float, CultureInfo.InvariantCulture, out sz) || !int.TryParse(layers.Text, out ly)
                    || !int.TryParse(kv.Text, out kh) || !int.TryParse(head.Text, out hd) || !int.TryParse(ctx.Text, out cx))
                {
                    MessageBox.Show(this, "数値の入力が不正です");
                    DialogResult = DialogResult.None;
                    return;
                }
                var r = new JObject
                {
                    ["id"] = id.Text.Trim(), ["display_name"] = name.Text.Trim() == "" ? id.Text.Trim() : name.Text.Trim(),
                    ["kind"] = kind.SelectedItem.ToString(), ["repo"] = repo.Text.Trim(), ["pattern"] = pattern.Text.Trim(),
                    ["size_gb"] = sz, ["n_layers"] = (long)ly, ["n_kv_heads"] = (long)kh, ["head_dim"] = (long)hd, ["ctx"] = (long)cx,
                    ["moe"] = moe.Checked, ["license"] = license.Text.Trim(),
                    ["roles"] = new JArray { },
                };
                foreach (var ro in roles.Text.Split(',').Select(x => x.Trim()).Where(x => x != "")) ((JArray)r["roles"]).Add(ro);
                if (mmproj.Text.Trim() != "") r["mmproj_pattern"] = mmproj.Text.Trim();
                Result = r;
            };
        }
    }

    sealed class WorkersPage : AdminPage
    {
        readonly DataGridView workers, queue, jobs;
        public override bool AutoRefresh => true;

        public WorkersPage(MainForm main) : base(main, "ワーカー / キュー")
        {
            workers = Ui.Grid(("name", "ワーカー", 180), ("backend", "バックエンド", 110), ("avail", "利用可能", 70), ("loaded", "VRAM上のモデル", 0), ("busy", "実行中", 60), ("installed", "導入済みモデル", 0));
            queue = Ui.Grid(("pos", "順位", 50), ("user", "ユーザー", 110), ("kind", "種別", 70), ("model", "モデル", 180), ("class", "優先クラス", 90),
                ("prio", "ユーザー優先度", 90), ("state", "状態", 70), ("wait", "待ち時間", 80), ("run", "実行時間", 80), ("est", "推定時間", 80));
            jobs = Ui.Grid(("time", "開始", 100), ("user", "ユーザー", 110), ("kind", "種別", 90), ("status", "状態", 80), ("gpu", "GPU秒", 70), ("error", "エラー", 0));
            var t = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 3, ColumnCount = 1 };
            t.RowStyles.Add(new RowStyle(SizeType.Percent, 30));
            t.RowStyles.Add(new RowStyle(SizeType.Percent, 38));
            t.RowStyles.Add(new RowStyle(SizeType.Percent, 32));
            var wp = new Panel { Dock = DockStyle.Fill };
            wp.Controls.Add(workers);
            wp.Controls.Add(Ui.Toolbar(Ui.Btn("選択ワーカーを再起動 (アイドルモデルを解放)", (s, e) => RestartWorker())));
            var qp = new Panel { Dock = DockStyle.Fill };
            qp.Controls.Add(queue);
            qp.Controls.Add(Ui.Toolbar(Ui.Btn("選択ジョブをキャンセル", (s, e) => CancelJob(queue, "job_id")),
                Ui.Label("キューは公平性 (ユーザー別GPU時間) + エージング + 優先度 + モデル親和性で並びます。優先度はメンバー編集で変更できます。", null, Ui.Muted)));
            t.Controls.Add(Group("ワーカー (リソースユニット)", wp), 0, 0);
            t.Controls.Add(Group("GPUキュー", qp), 0, 1);
            t.Controls.Add(Group("最近のジョブ", jobs), 0, 2);
            Controls.Add(t);
        }

        public override async Task RefreshAsync()
        {
            var w = await Api.GetAsync("/api/admin/workers");
            Ui.Fill(workers, w.Arr("workers").Objects(), x => new object[]
            {
                x.Str("name"), x.Str("backend"), x.Bool("available") ? "✓" : "✗", string.Join(", ", x.Arr("loaded").Cast<object>()),
                x.Int("busy"), string.Join(", ", x.Arr("installed").Cast<object>()),
            }, (row, x) => row.Cells["avail"].Style.ForeColor = x.Bool("available") ? Ui.Ok : Ui.Bad);
            var q = await Api.GetAsync("/api/admin/queue");
            Ui.Fill(queue, q.Arr("units").Objects(), x => new object[]
            {
                x.Int("position") == 0 ? "実行中" : x.Int("position").ToString(), x.Str("username"), x.Str("kind"), x.Str("model_id"),
                x.Str("priority_class"), x.Int("user_priority"), x.Str("state") == "running" ? "実行中" : "待機", $"{x.Num("waited_seconds"):F0}s",
                $"{x.Num("running_seconds"):F0}s", $"{x.Num("est_seconds"):F0}s",
            });
            var j = await Api.GetAsync("/api/admin/jobs?limit=60");
            Ui.Fill(jobs, j.Arr("jobs").Objects(), x => new object[]
            {
                Ui.Time(x.Num("created_at")), x.Str("username"), x.Str("kind"), x.Str("status"), $"{x.Num("gpu_seconds"):F1}", x.Str("error"),
            }, (row, x) => row.Cells["status"].Style.ForeColor = x.Str("status") == "failed" ? Ui.Bad : x.Str("status") == "done" ? Ui.Ok : Ui.Muted);
        }

        void RestartWorker()
        {
            var w = RequireSelection(workers, "ワーカー");
            if (w == null) return;
            Run(async () =>
            {
                var r = await Api.PostAsync($"/api/admin/workers/{w.Str("id")}/restart");
                MessageBox.Show(this, $"解放: {string.Join(", ", r.Arr("stopped").Cast<object>())}\n{r.Str("note")}", "ワーカー再起動");
            });
        }

        void CancelJob(DataGridView g, string key)
        {
            var r = RequireSelection(g, "ジョブ");
            if (r == null || !Ui.Confirm(this, "このジョブをキャンセルしますか？")) return;
            Run(() => Api.PostAsync($"/api/admin/jobs/{r.Str(key)}/cancel"));
        }
    }

    sealed class SettingsPage : AdminPage
    {
        readonly DataGridView grid;
        readonly ComboBox section;
        readonly Label help;
        JArray all = new JArray();

        static readonly (string id, string text)[] Sections =
        {
            ("profile", "Dynamic Profile (速度特化 / 精度特化 / 自律特化 の内部チューニング)"),
            ("scheduler", "GPUスケジューラ / キュー"), ("resources", "リソース管理 (VRAM/RAM/温度/ストレージ)"), ("models", "モデル / キャッシュ"),
            ("sandbox", "コードサンドボックス"), ("tools", "外部ツール (ffmpeg / pandoc)"), ("web", "Web検索 / ブラウジング"), ("generation", "画像・動画・音楽の生成制限"),
            ("api", "OpenAI互換API / メンバーのAPIキー"),
            ("auth", "認証 / セッション / 信頼端末"), ("users", "新規メンバーの既定値"), ("storage", "ストレージ"), ("server", "サーバー (再起動が必要)"),
        };

        static readonly string[] Help =
        {
            "速度特化 (speed) / 精度特化 (balanced) / 自律特化 (autonomous) の各値は、連続値 t (0〜1) の基準点です。利用者が選んだモードの範囲内で t を自動決定し、上限値をこの表の間で補間します。混雑時は congestion_shift だけ速度寄りに、余裕があれば idle_boost だけ上げます。精度特化・自律特化は混雑しても下げずに待機します。",
            "aging_per_second: 待ち時間1秒あたりの加点 (飢餓防止)。max_wait_force_seconds を超えたジョブは最優先で実行されます。fair_share_*: 直近のGPU利用時間が多いユーザーほど後回し。swap_patience_seconds: ロード済みモデルの仕事がある間、スワップを待つ時間。",
            "vram_reserve_mb: Windows/他アプリ用に常に空けるVRAM。host_friendly: ゲーム等が使うVRAMを予算から除外。ram_*_mb: 空きRAMがこの値を下回ると段階的に負荷を下げます。disk_margin_gb: 常に確保するディスク空き容量。",
            "kv_cache_type: KVキャッシュ量子化 (q8_0推奨)。cache_reuse: プレフィックスキャッシュ再利用。idle_unload_seconds: 未使用モデルを解放するまでの時間。prefetch: 次に使うモデルをRAMへ先読み (スラッシング時は自動停止)。",
            "コード実行は WASM (python.wasm) で隔離されます。ネットワーク・プロセス生成は不可、メモリ/時間/ディスク容量を制限します。",
            "auto_install: 必要になったときに公式リリース (GitHub, SHA-256 検証) から自動ダウンロードするか。allowed: AI が使ってよいツール (ffmpeg: 動画・音声の変換、pandoc: 文書形式の変換)。timeout_seconds: 1回の実行の上限。ツールはサンドボックス作業ディレクトリ内のファイルにだけ、NextAI が組み立てた引数で実行されます。",
            "SSRF対策として localhost・LAN・内部アドレスへのアクセスは常に遮断されます。search_provider: duckduckgo / searxng / brave。",
            "動画生成はローカルGPU向けに尺と解像度を制限しています。コストは生成クォータの消費量です。",
            "enabled: /v1 (OpenAI互換API) 全体の有効/無効。member_keys: メンバーが設定画面で自分のAPIキーを発行できるか。key_max_days: キーの最長有効日数。max_keys_per_user: 1人あたりの有効キー数。max_tokens_cap: 1回の応答の最大トークン数。APIからの利用もGPUキュー・クォータ・レート制限の対象です。発行済みキーの一覧と失効は「メンバー」→「APIトークン」から。",
            "セッション・信頼端末の有効期限、ログイン試行制限などを設定します。",
            "新しく作成するメンバーの既定クォータです (個別の値はメンバー画面で変更)。",
            "一時ファイルやバックアップの保持設定です。",
            "ポート・TLS・外部公開URLなど。変更後はサーバー再起動が必要です。allow_remote_admin は既定で無効 (管理APIはこのPCからのみ)。",
        };

        public SettingsPage(MainForm main) : base(main, "AI設定")
        {
            section = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 420 };
            foreach (var s in Sections) section.Items.Add(s.text);
            section.SelectedIndex = 0;
            section.SelectedIndexChanged += (s, e) => ShowSection();
            grid = Ui.Grid(("key", "設定キー", 300), ("value", "値", 180), ("default", "既定値", 160), ("type", "型", 60), ("source", "設定元", 70), ("restart", "再起動", 60));
            grid.ReadOnly = false;
            foreach (DataGridViewColumn c in grid.Columns) c.ReadOnly = c.Name != "value";
            grid.Columns["value"].DefaultCellStyle.BackColor = Color.FromArgb(255, 252, 235);
            help = new Label { Dock = DockStyle.Top, Height = 58, ForeColor = Ui.Muted, Padding = new Padding(6) };
            var bar = Ui.Toolbar(Ui.Label("カテゴリ:"), section, Ui.Btn("変更を保存", (s, e) => Save(), true), Ui.Btn("選択項目を既定値に戻す", (s, e) => Reset()), Ui.Btn("再読み込み", (s, e) => Run(() => Task.CompletedTask)));
            Controls.Add(grid);
            Controls.Add(help);
            Controls.Add(bar);
        }

        public override async Task RefreshAsync()
        {
            var d = await Api.GetAsync("/api/admin/settings");
            all = d.Arr("settings");
            ShowSection();
        }

        void ShowSection()
        {
            var sec = Sections[section.SelectedIndex].id;
            help.Text = Help[section.SelectedIndex];
            Ui.Fill(grid, all.Objects().Where(x => x.Str("section") == sec), x => new object[]
            {
                x.Str("key"), Val(x, "value"), Val(x, "default"), x.Str("type"), x.Str("source") == "admin" ? "管理者" : x.Str("source") == "file" ? "設定ファイル" : "既定", x.Bool("restart_required") ? "要" : "",
            }, (row, x) => { if (x.Str("source") == "admin") row.Cells["source"].Style.ForeColor = Ui.Accent; });
        }

        static string Val(JObject x, string k)
        {
            if (!x.ContainsKey(k)) return "";
            var v = x[k];
            return v is JArray a ? string.Join(",", a.Cast<object>()) : x.Str(k);
        }

        void Save()
        {
            grid.EndEdit();
            var values = new JObject();
            foreach (DataGridViewRow row in grid.Rows)
            {
                var x = row.Tag as JObject;
                var now = Convert.ToString(row.Cells["value"].Value) ?? "";
                if (x != null && now != Val(x, "value") && !(x.Bool("secret") && now == "********")) values[x.Str("key")] = now;
            }
            if (values.Count == 0) { MessageBox.Show(this, "変更はありません。"); return; }
            Run(async () =>
            {
                var r = await Api.PutAsync("/api/admin/settings", new JObject { ["values"] = values });
                MessageBox.Show(this, r.Bool("restart_required") ? "保存しました。一部の設定はサーバー再起動後に反映されます (サーバータブ → 再起動)。" : "保存しました。即時反映されます。", "AI設定");
            });
        }

        void Reset()
        {
            var x = RequireSelection(grid, "設定");
            if (x == null) return;
            Run(() => Api.PutAsync("/api/admin/settings", new JObject { ["reset"] = new JArray { x.Str("key") } }));
        }
    }
}
