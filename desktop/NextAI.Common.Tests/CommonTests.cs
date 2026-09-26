using System.Linq;
using NextAI.Common;
using Xunit;

public class JsonTests
{
    [Fact]
    public void ParsesNestedStructures()
    {
        var o = Json.ParseObject("{\"a\":1,\"b\":[true,null,2.5,\"x\\u3042\\n\"],\"c\":{\"d\":-3e2},\"e\":\"日本語\"}");
        Assert.Equal(1, o.Int("a"));
        var b = o.Arr("b");
        Assert.Equal(true, b[0]);
        Assert.Null(b[1]);
        Assert.Equal(2.5, (double)b[2]);
        Assert.Equal("xあ\n", b[3]);
        Assert.Equal(-300, o.Obj("c").Num("d"));
        Assert.Equal("日本語", o.Str("e"));
        Assert.Equal("", o.Str("missing"));
        Assert.Equal(7, o.Int("missing", 7));
    }

    [Fact]
    public void RoundTrips()
    {
        var o = new JObject { ["s"] = "q\"uote\\", ["n"] = 42L, ["f"] = 1.5, ["b"] = false, ["a"] = new JArray { 1L, "two" }, ["nul"] = null };
        var back = Json.ParseObject(Json.Serialize(o));
        Assert.Equal("q\"uote\\", back.Str("s"));
        Assert.Equal(42, back.Int("n"));
        Assert.Equal(1.5, back.Num("f"));
        Assert.False(back.Bool("b", true));
        Assert.Equal(2, back.Arr("a").Count);
        Assert.True(back.IsNull("nul"));
    }

    [Theory]
    [InlineData("{")]
    [InlineData("[1,]")]
    [InlineData("{\"a\" 1}")]
    [InlineData("tru")]
    [InlineData("\"unterminated")]
    [InlineData("{} x")]
    public void RejectsMalformed(string text) => Assert.ThrowsAny<System.FormatException>(() => Json.Parse(text));

    [Fact]
    public void RejectsDeepNesting() =>
        Assert.ThrowsAny<System.FormatException>(() => Json.Parse(new string('[', 300) + new string(']', 300)));

    [Fact]
    public void ParsesServerProgressEvent()
    {
        var ev = Json.ParseObject("{\"event\": \"progress\", \"model\": \"qwen3-4b-instruct\", \"file\": \"a.gguf\", \"done\": 1048576, \"size\": 2621440000, \"overall_done\": 1048576, \"overall_total\": 40000000000}");
        Assert.Equal("progress", ev.Str("event"));
        Assert.Equal(40000000000d, ev.Num("overall_total"));
    }
}

public class ProcessRunnerTests
{
    [Theory]
    [InlineData("simple", "simple")]
    [InlineData("with space", "\"with space\"")]
    [InlineData("", "\"\"")]
    [InlineData("C:\\Program Files\\NextAI\\", "\"C:\\Program Files\\NextAI\\\\\"")]
    [InlineData("say \"hi\"", "\"say \\\"hi\\\"\"")]
    public void QuotesLikeCommandLineToArgv(string input, string expected) => Assert.Equal(expected, ProcessRunner.Quote(input));

    [Fact]
    public void StartFailureIsReportedWithCode()
    {
        var ex = Assert.ThrowsAny<ProcessStartException>(() =>
            ProcessRunner.RunAsync("/nonexistent/dir/uv.exe", new string[0]).GetAwaiter().GetResult());
        Assert.Equal(2, ex.Code);
        Assert.False(ex.Transient);
        Assert.Contains("uv.exe", ex.Message);
        Assert.NotEqual("", ex.Hint);
    }

    [Fact]
    public void JoinsArguments() =>
        Assert.Equal("-m nextai --data-dir \"C:\\Program Data\\x\"", ProcessRunner.JoinArgs(new[] { "-m", "nextai", "--data-dir", "C:\\Program Data\\x" }));
}

public class VersionTests
{
    [Theory]
    [InlineData("1.0.0", "1.0.0", 0)]
    [InlineData("1.2.0", "1.10.0", -1)]
    [InlineData("2.0", "1.9.9", 1)]
    [InlineData("1.0", "1.0.1", -1)]
    public void ComparesVersions(string a, string b, int sign) => Assert.Equal(sign, System.Math.Sign(InstallInfo.CompareVersions(a, b)));
}
