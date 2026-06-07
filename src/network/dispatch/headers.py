CHROME_VERSION = "136"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{CHROME_VERSION}.0.0.0 Safari/537.36"
)

SEC_CH_UA = (
    f'"Google Chrome";v="{CHROME_VERSION}", '
    f'"Chromium";v="{CHROME_VERSION}", '
    '"Not_A Brand";v="99"'
)

SEC_FETCH_HEADERS: dict[str, str] = {
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}
