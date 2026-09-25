using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Management;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using Microsoft.Win32;

namespace NextAI.Common
{
    public sealed class ProbeCheck
    {
        public string Id, Name, Status, Value, Advice;
        public ProbeCheck(string id, string name, string status, string value, string advice = "")
        { Id = id; Name = name; Status = status; Value = value; Advice = advice; }
    }

    /// <summary>Pre-install hardware/OS inspection (runs before Python exists on the machine).</summary>
    public sealed class SystemProbe
    {
        public int WindowsBuild;
        public string WindowsText = "";
        public bool Is64Bit;
        public string CpuName = "";
        public int CpuThreads;
        public double RamGb;
        public string GpuName = "";
        public double VramGb;
        public string DriverVersion = "";
        public string CudaVersion = "";
        public double DiskFreeGb;
        public bool WslAvailable;
        public bool DockerAvailable;
        public bool PortFree = true;
        public bool Internet;

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Auto)]
        class MEMORYSTATUSEX
        {
            public uint dwLength = (uint)Marshal.SizeOf(typeof(MEMORYSTATUSEX));
            public uint dwMemoryLoad;
            public ulong ullTotalPhys, ullAvailPhys, ullTotalPageFile, ullAvailPageFile, ullTotalVirtual, ullAvailVirtual, ullAvailExtendedVirtual;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
        static extern bool GlobalMemoryStatusEx([In, Out] MEMORYSTATUSEX lpBuffer);

        public static SystemProbe Run(string dataDir, int port)
        {
            var p = new SystemProbe { Is64Bit = Environment.Is64BitOperatingSystem, CpuThreads = Environment.ProcessorCount };
            try
            {
                using (var k = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion"))
                {
                    p.WindowsBuild = int.Parse(Convert.ToString(k?.GetValue("CurrentBuild") ?? "0"), CultureInfo.InvariantCulture);
                    var name = p.WindowsBuild >= 22000 ? "Windows 11" : "Windows 10";
                    p.WindowsText = $"{name} {k?.GetValue("EditionID")} {k?.GetValue("DisplayVersion")} (Build {p.WindowsBuild})";
                }
            }
            catch { p.WindowsText = Environment.OSVersion.VersionString; }
            try
            {
                using (var s = new ManagementObjectSearcher("SELECT Name FROM Win32_Processor"))
                    foreach (var o in s.Get()) { p.CpuName = Convert.ToString(o["Name"]).Trim(); break; }
            }
            catch { }
            var mem = new MEMORYSTATUSEX();
            if (GlobalMemoryStatusEx(mem)) p.RamGb = mem.ullTotalPhys / 1073741824.0;
            ProbeGpu(p);
            try
            {
                var root = Path.GetPathRoot(Path.GetFullPath(dataDir));
                p.DiskFreeGb = new DriveInfo(root).AvailableFreeSpace / 1073741824.0;
            }
            catch { }
            p.WslAvailable = Run("wsl.exe", "--status", 15) == 0;
            p.DockerAvailable = Run("docker", "--version", 10) == 0;
            try
            {
                var l = new TcpListener(IPAddress.Any, port);
                l.Start();
                l.Stop();
            }
            catch (SocketException) { p.PortFree = false; }
            p.Internet = CanReach("https://pypi.org/simple/") && CanReach("https://huggingface.co/");
            return p;
        }

        static void ProbeGpu(SystemProbe p)
        {
            var smi = FindNvidiaSmi();
            if (smi != null)
            {
                var o = Capture(smi, "--query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits", 20);
                var line = (o ?? "").Split('\n')[0].Split(',');
                if (line.Length >= 3)
                {
                    p.GpuName = line[0].Trim();
                    double mb;
                    if (double.TryParse(line[1].Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out mb)) p.VramGb = mb / 1024.0;
                    p.DriverVersion = line[2].Trim();
                }
                var head = Capture(smi, "", 20) ?? "";
                var m = Regex.Match(head, @"CUDA Version:\s*([\d.]+)");
                if (m.Success) p.CudaVersion = m.Groups[1].Value;
            }
            if (string.IsNullOrEmpty(p.GpuName))
            {
                try
                {
                    using (var s = new ManagementObjectSearcher("SELECT Name FROM Win32_VideoController"))
                        foreach (var o in s.Get()) { p.GpuName = Convert.ToString(o["Name"]); break; }
                }
                catch { }
            }
        }

        static string FindNvidiaSmi()
        {
            foreach (var c in new[] {
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "nvidia-smi.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), @"NVIDIA Corporation\NVSMI\nvidia-smi.exe") })
                if (File.Exists(c)) return c;
            return null;
        }

        static int Run(string exe, string args, int timeoutSec)
        {
            try
            {
                using (var pr = Process.Start(new ProcessStartInfo(exe, args) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true }))
                {
                    if (!pr.WaitForExit(timeoutSec * 1000)) { try { pr.Kill(); } catch { } return -1; }
                    return pr.ExitCode;
                }
            }
            catch { return -1; }
        }

        static string Capture(string exe, string args, int timeoutSec)
        {
            try
            {
                using (var pr = Process.Start(new ProcessStartInfo(exe, args) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true }))
                {
                    var o = pr.StandardOutput.ReadToEnd();
                    pr.WaitForExit(timeoutSec * 1000);
                    return o;
                }
            }
            catch { return null; }
        }

        static bool CanReach(string url)
        {
            try
            {
                ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
                var req = (HttpWebRequest)WebRequest.Create(url);
                req.Method = "HEAD";
                req.Timeout = 8000;
                using (var r = (HttpWebResponse)req.GetResponse()) return (int)r.StatusCode < 500;
            }
            catch (WebException e) when (e.Response is HttpWebResponse hr) { return (int)hr.StatusCode < 500; }
            catch { return false; }
        }

        static int Major(string v)
        {
            int x;
            return int.TryParse((v ?? "").Split('.')[0], out x) ? x : 0;
        }

        public List<ProbeCheck> Evaluate(double requiredDiskGb, double marginGb)
        {
            var list = new List<ProbeCheck>
            {
                new ProbeCheck("os", "Windows", !Is64Bit ? "FAIL" : WindowsBuild >= 22000 ? "PASS" : WindowsBuild >= 19045 ? "WARN" : "FAIL",
                    WindowsText, WindowsBuild >= 22000 ? "" : "Windows 11 (64bit) を推奨します"),
                new ProbeCheck("cpu", "CPU", CpuThreads >= 8 ? "PASS" : "WARN", $"{CpuName} ({CpuThreads} スレッド)"),
                new ProbeCheck("ram", "RAM", RamGb >= 28 ? "PASS" : RamGb >= 15 ? "WARN" : "FAIL", $"{RamGb:F1} GB",
                    RamGb >= 28 ? "" : "RAMが少ないため大型モデルの利用が制限されます"),
            };
            var isNvidia = GpuName.IndexOf("NVIDIA", StringComparison.OrdinalIgnoreCase) >= 0 || GpuName.IndexOf("GeForce", StringComparison.OrdinalIgnoreCase) >= 0 || VramGb > 0;
            list.Add(new ProbeCheck("gpu", "GPU", isNvidia ? "PASS" : "WARN", string.IsNullOrEmpty(GpuName) ? "検出できません" : GpuName,
                isNvidia ? "" : "NVIDIA GPU が見つかりません。CPUのみの軽量構成になります"));
            if (isNvidia)
            {
                list.Add(new ProbeCheck("vram", "VRAM", VramGb >= 10.5 ? "PASS" : VramGb >= 6 ? "WARN" : "FAIL", $"{VramGb:F1} GB"));
                var blackwell = Regex.IsMatch(GpuName, @"RTX\s*50\d\d");
                var needDriver = blackwell ? 570 : 535;
                list.Add(new ProbeCheck("driver", "NVIDIA Driver", Major(DriverVersion) >= needDriver ? "PASS" : "WARN", DriverVersion == "" ? "不明" : DriverVersion,
                    Major(DriverVersion) >= needDriver ? "" : $"NVIDIA ドライバ {needDriver} 以降に更新してください"));
                list.Add(new ProbeCheck("cuda", "CUDA (ドライバ対応)", CudaVersion == "" ? "WARN" : "PASS", CudaVersion == "" ? "不明" : CudaVersion));
            }
            list.Add(new ProbeCheck("disk", "ストレージ空き容量", DiskFreeGb >= requiredDiskGb + marginGb ? "PASS" : DiskFreeGb >= 25 + marginGb ? "WARN" : "FAIL",
                $"{DiskFreeGb:F1} GB (必要: 約{requiredDiskGb:F0} GB + 安全マージン {marginGb:F0} GB)",
                DiskFreeGb >= requiredDiskGb + marginGb ? "" : "容量に合わせて小さいモデルセットを選択してください"));
            list.Add(new ProbeCheck("port", "ポート", PortFree ? "PASS" : "WARN", PortFree ? "利用可能" : "使用中 (既存インストールの可能性)",
                PortFree ? "" : "別のポートを指定するか、既存のサービスを確認してください"));
            list.Add(new ProbeCheck("net", "インターネット接続", Internet ? "PASS" : "FAIL", Internet ? "OK" : "PyPI / Hugging Face に接続できません",
                Internet ? "" : "モデルとランタイムのダウンロードにインターネット接続が必要です"));
            list.Add(new ProbeCheck("wsl", "WSL2", WslAvailable ? "PASS" : "SKIP", WslAvailable ? "利用可能" : "未構成", "既定構成では不要です (ネイティブ実行)"));
            list.Add(new ProbeCheck("docker", "Docker", DockerAvailable ? "PASS" : "SKIP", DockerAvailable ? "利用可能" : "未インストール", "既定構成では不要です"));
            return list;
        }
    }
}
