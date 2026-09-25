using System;
using System.IO;

namespace NextAI.Common
{
    /// <summary>Paths and settings written by the installer to install.json next to the executables.</summary>
    public sealed class InstallInfo
    {
        public const string ServiceName = "NextAIServer";
        public const string ProductName = "NextAI Platform";
        public const string RegistryKey = @"SOFTWARE\NextAI\Platform";
        public const string UninstallKey = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\NextAIPlatform";

        public string Version = "";
        public string InstallDir = "";
        public string DataDir = "";
        public string Python = "";
        public string ServerDir = "";
        public int Port = 8443;
        public string ModelSet = "";

        public string BaseUrl => $"https://127.0.0.1:{Port}";
        public string CaCertPath => Path.Combine(InstallDir, "ca.crt");
        public string InstallJsonPath => Path.Combine(InstallDir, "install.json");

        public static string DefaultInstallDir =>
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "NextAI");

        public static string DefaultDataDir =>
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "NextAI");

        public static InstallInfo Load(string path)
        {
            var o = Json.ParseObject(File.ReadAllText(path));
            return new InstallInfo
            {
                Version = o.Str("version"),
                InstallDir = o.Str("install_dir"),
                DataDir = o.Str("data_dir"),
                Python = o.Str("python"),
                ServerDir = o.Str("server_dir"),
                Port = (int)o.Int("port", 8443),
                ModelSet = o.Str("model_set"),
            };
        }

        public static InstallInfo LoadNextToExe()
        {
            var dir = AppDomain.CurrentDomain.BaseDirectory;
            var p = Path.Combine(dir, "install.json");
            if (File.Exists(p)) return Load(p);
            return new InstallInfo { InstallDir = dir.TrimEnd('\\', '/'), DataDir = DefaultDataDir };
        }

        public void Save()
        {
            var o = new JObject
            {
                ["version"] = Version, ["install_dir"] = InstallDir, ["data_dir"] = DataDir, ["python"] = Python,
                ["server_dir"] = ServerDir, ["port"] = Port, ["model_set"] = ModelSet,
            };
            File.WriteAllText(InstallJsonPath, Json.Serialize(o));
        }

        public static int CompareVersions(string a, string b)
        {
            var pa = (a ?? "0").Split('.');
            var pb = (b ?? "0").Split('.');
            for (var i = 0; i < Math.Max(pa.Length, pb.Length); i++)
            {
                int x = 0, y = 0;
                if (i < pa.Length) int.TryParse(pa[i], out x);
                if (i < pb.Length) int.TryParse(pb[i], out y);
                if (x != y) return x.CompareTo(y);
            }
            return 0;
        }
    }
}
