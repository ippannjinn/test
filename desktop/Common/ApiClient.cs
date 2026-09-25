using System;
using System.Net;
using System.Net.Http;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Threading.Tasks;

namespace NextAI.Common
{
    public sealed class ApiException : Exception
    {
        public int Status { get; }
        public string Code { get; }
        public ApiException(int status, string code, string message) : base(message) { Status = status; Code = code; }
    }

    /// <summary>Admin API client. TLS is validated against the platform's own local CA (certificate pinning).</summary>
    public sealed class ApiClient : IDisposable
    {
        readonly HttpClient http;
        readonly X509Certificate2 pinnedCa;
        string token;

        public string BaseUrl { get; }
        public bool LoggedIn => token != null;
        public JObject User { get; private set; }

        public ApiClient(string baseUrl, string caCertPath)
        {
            BaseUrl = baseUrl.TrimEnd('/');
            ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
            if (!string.IsNullOrEmpty(caCertPath) && System.IO.File.Exists(caCertPath))
                pinnedCa = new X509Certificate2(caCertPath);
            var handler = new HttpClientHandler { UseProxy = false, UseCookies = false, ServerCertificateCustomValidationCallback = Validate };
            http = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(120) };
            http.DefaultRequestHeaders.UserAgent.ParseAdd("NextAI-Admin/1.0");
        }

        bool Validate(HttpRequestMessage req, X509Certificate2 cert, X509Chain chain, SslPolicyErrors errors)
        {
            if (errors == SslPolicyErrors.None) return true;
            if (pinnedCa == null || cert == null) return false;
            if ((errors & SslPolicyErrors.RemoteCertificateNameMismatch) != 0) return false;
            using (var ch = new X509Chain())
            {
                ch.ChainPolicy.RevocationMode = X509RevocationMode.NoCheck;
                ch.ChainPolicy.VerificationFlags = X509VerificationFlags.AllowUnknownCertificateAuthority;
                ch.ChainPolicy.ExtraStore.Add(pinnedCa);
                if (!ch.Build(cert)) return false;
                var root = ch.ChainElements[ch.ChainElements.Count - 1].Certificate;
                return string.Equals(root.Thumbprint, pinnedCa.Thumbprint, StringComparison.OrdinalIgnoreCase);
            }
        }

        public async Task<JObject> LoginAsync(string username, string password)
        {
            var res = await SendAsync(HttpMethod.Post, "/api/auth/login",
                new JObject { ["username"] = username, ["password"] = password, ["client"] = "admin_app" }).ConfigureAwait(false);
            token = res.Str("token");
            User = res.Obj("user");
            return res;
        }

        public async Task LogoutAsync()
        {
            if (token == null) return;
            try { await SendAsync(HttpMethod.Post, "/api/auth/logout", new JObject()).ConfigureAwait(false); } catch { }
            token = null;
        }

        public Task<JObject> GetAsync(string path) => SendAsync(HttpMethod.Get, path, null);
        public Task<JObject> PostAsync(string path, object body = null) => SendAsync(HttpMethod.Post, path, body ?? new JObject());
        public Task<JObject> PutAsync(string path, object body) => SendAsync(HttpMethod.Put, path, body);
        public Task<JObject> PatchAsync(string path, object body) => SendAsync(new HttpMethod("PATCH"), path, body);
        public Task<JObject> DeleteAsync(string path, object body = null) => SendAsync(HttpMethod.Delete, path, body);

        public async Task<bool> HealthAsync()
        {
            try
            {
                using (var r = await http.GetAsync(BaseUrl + "/api/health").ConfigureAwait(false))
                    return r.IsSuccessStatusCode;
            }
            catch { return false; }
        }

        async Task<JObject> SendAsync(HttpMethod method, string path, object body)
        {
            using (var req = new HttpRequestMessage(method, BaseUrl + path))
            {
                if (token != null) req.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", token);
                if (body != null) req.Content = new StringContent(Json.Serialize(body), Encoding.UTF8, "application/json");
                HttpResponseMessage resp;
                try { resp = await http.SendAsync(req).ConfigureAwait(false); }
                catch (HttpRequestException ex) { throw new ApiException(0, "connection", "サーバーに接続できません: " + (ex.InnerException?.Message ?? ex.Message)); }
                catch (TaskCanceledException) { throw new ApiException(0, "timeout", "サーバーの応答がタイムアウトしました"); }
                using (resp)
                {
                    var text = await resp.Content.ReadAsStringAsync().ConfigureAwait(false);
                    JObject obj;
                    try { obj = string.IsNullOrWhiteSpace(text) ? new JObject() : Json.ParseObject(text); }
                    catch (FormatException) { obj = new JObject(); }
                    if (!resp.IsSuccessStatusCode)
                    {
                        var err = obj.Obj("error");
                        if ((int)resp.StatusCode == 401) token = null;
                        throw new ApiException((int)resp.StatusCode, err.Str("code", "http_" + (int)resp.StatusCode),
                            err.Str("message", "HTTP " + (int)resp.StatusCode));
                    }
                    return obj;
                }
            }
        }

        public void Dispose() => http.Dispose();
    }
}
