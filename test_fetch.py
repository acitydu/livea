"""测试腾讯行情API连接"""
import urllib.request
import ssl

def test_tencent():
    # 测试HTTP
    print("=== Testing HTTP ===")
    url = "http://qt.gtimg.cn/?q=s_sh000001"
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://finance.qq.com'
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
            print(f"HTTP Status: {resp.status}")
            print(f"Raw bytes (first 200): {data[:200]}")
            text = data.decode('gbk')
            print(f"GBK decoded: {text[:200]}")
            return True
    except Exception as e:
        print(f"HTTP Failed: {type(e).__name__}: {e}")

    # 测试HTTPS with SSL bypass
    print("\n=== Testing HTTPS (SSL bypass) ===")
    url = "https://qt.gtimg.cn/?q=s_sh000001"
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://finance.qq.com'
        })
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            data = resp.read()
            print(f"HTTPS Status: {resp.status}")
            text = data.decode('gbk')
            print(f"GBK decoded: {text[:200]}")
            return True
    except Exception as e:
        print(f"HTTPS Failed: {type(e).__name__}: {e}")

    return False

if __name__ == "__main__":
    test_tencent()
    input("\nPress Enter to exit...")
