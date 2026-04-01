#!/usr/bin/env python3
"""SRT 빈자리 모니터 - SRTplay 기반 열차 예매 감시 도구"""

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── 상수 ──────────────────────────────────────────────────────

URL = "https://srtplay.com/ticket/reservation/schedule/proc"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_stations():
    path = os.path.join(SCRIPT_DIR, "station.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {item["codeNm"]: item["codeVal"] for item in data if item.get("isUse") == "Y"}

STATIONS = _load_stations()

WEEKDAYS_KR = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]

SOLD_OUT_KEYWORDS = {"매진", "0", "", "soldout", "N"}


# ─── API ───────────────────────────────────────────────────────

# 세션은 런타임에 갱신될 수 있으므로 전역으로 관리
_session_value = None


def get_session():
    global _session_value
    if _session_value is None:
        _session_value = os.getenv("SESSION")
    return _session_value


def set_session(new_session):
    global _session_value
    _session_value = new_session
    print(f"  🔄 세션 갱신됨: {new_session[:16]}...")


def get_cookies():
    token = os.getenv("XSRF_TOKEN")
    remember = os.getenv("REMEMBER_ME")
    session = get_session()
    if not all([token, remember, session]):
        print("❌ .env 파일에 XSRF_TOKEN, REMEMBER_ME, SESSION을 설정해주세요.")
        print("   .env.example 참고")
        sys.exit(1)
    return {
        "XSRF-TOKEN": token,
        "remember-me": remember,
        "SESSION": session,
    }


def build_form_data(dpt_name, arv_name, date_str, dpt_tm="0", passengers=None):
    """검색 파라미터 생성."""
    if passengers is None:
        passengers = [1, 0, 0, 0, 0]

    dpt_code = STATIONS.get(dpt_name)
    arv_code = STATIONS.get(arv_name)
    if not dpt_code or not arv_code:
        print(f"❌ 역 이름을 확인해주세요. 사용 가능한 역:")
        print("   " + ", ".join(STATIONS.keys()))
        sys.exit(1)

    dt = datetime.strptime(date_str, "%Y%m%d")
    day_of_week = WEEKDAYS_KR[dt.weekday()]
    dpt_dt_txt = f"{dt.year}.+{dt.month}.+{dt.day}."

    return {
        "_csrf": os.getenv("XSRF_TOKEN"),
        "passenger1": str(passengers[0]),
        "passenger2": str(passengers[1]),
        "passenger3": str(passengers[2]),
        "passenger4": str(passengers[3]),
        "passenger5": str(passengers[4]),
        "handicapSeatType": "015",
        "selectScheduleData": "",
        "psrmClCd": "",
        "isGroup": "",
        "isCash": "",
        "dptRsStnCd": dpt_code,
        "dptRsStnNm": dpt_name,
        "arvRsStnCd": arv_code,
        "arvRsStnNm": arv_name,
        "dptDt": date_str,
        "dptTm": dpt_tm,
        "dptDtTxt": dpt_dt_txt,
        "dptDayOfWeekTxt": day_of_week,
    }


def _do_request(dpt_name, arv_name, date_str, dpt_tm="0", passengers=None):
    """단일 API 요청 실행. (resp 객체 반환)"""
    headers = {
        "Accept": "*/*",
        "Accept-Language": "ko,en-US;q=0.9,en;q=0.8,ja;q=0.7",
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    cookies = get_cookies()
    data = build_form_data(dpt_name, arv_name, date_str, dpt_tm, passengers)
    return requests.post(URL, headers=headers, cookies=cookies, data=data,
                         allow_redirects=False, timeout=10)


def _extract_session_from_response(resp):
    """응답의 Set-Cookie 헤더에서 새 SESSION 값 추출."""
    for cookie in resp.cookies:
        if cookie.name == "SESSION":
            return cookie.value
    return None


def _fetch_page(dpt_name, arv_name, date_str, dpt_tm="0", passengers=None):
    """단일 페이지 요청. 세션 만료 시 자동 갱신. HTML 텍스트 반환."""
    resp = _do_request(dpt_name, arv_name, date_str, dpt_tm, passengers)

    # 세션 만료 → Set-Cookie에서 새 세션 추출 후 재시도
    if resp.status_code == 302:
        new_session = _extract_session_from_response(resp)
        if new_session:
            set_session(new_session)
            resp = _do_request(dpt_name, arv_name, date_str, dpt_tm, passengers)
        else:
            print("❌ 세션이 만료되었고 자동 갱신에 실패했습니다.")
            print("   브라우저에서 쿠키를 다시 복사해 .env를 갱신해주세요.")
            sys.exit(1)

    if resp.status_code == 302:
        print("❌ 세션 갱신 후에도 302 응답. .env 쿠키를 모두 갱신해주세요.")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"❌ 요청 실패: HTTP {resp.status_code}")
        print(resp.text[:300])
        sys.exit(1)

    return resp.text


def _get_next_page_info(html_text):
    """HTML에서 다음 페이지 존재 여부와 lastDptTm을 추출."""
    fllw = re.search(r'class="fllwPgExt"[^>]*>(\w+)<', html_text)
    last_tm = re.search(r'class="lastDptTm"[^>]*>(\d+)<', html_text)
    has_next = fllw and fllw.group(1) == "Y"
    next_tm = last_tm.group(1) if last_tm else None
    return has_next, next_tm


def fetch_schedule(dpt_name, arv_name, date_str, passengers=None):
    """SRTplay에서 전체 열차 스케줄 조회. 페이지네이션 자동 처리. HTML 리스트 반환."""
    all_html = []
    dpt_tm = "0"
    max_pages = 10  # 무한루프 방지

    for _ in range(max_pages):
        html_text = _fetch_page(dpt_name, arv_name, date_str, dpt_tm, passengers)
        all_html.append(html_text)

        has_next, next_tm = _get_next_page_info(html_text)
        if not has_next or not next_tm:
            break
        dpt_tm = next_tm

    return "\n".join(all_html)


# ─── 파싱 ──────────────────────────────────────────────────────

def _parse_java_map(s):
    """'{key1=val1, key2=val2, ...}' 형식의 Java-map 문자열을 dict로 변환."""
    s = s.strip().strip("{}")
    result = {}
    # key=value 쌍을 파싱. value에 '='가 들어갈 수 있으므로 key 기준으로 분리
    parts = re.split(r",\s*(?=\w+=)", s)
    for part in parts:
        eq = part.find("=")
        if eq == -1:
            continue
        key = part[:eq].strip()
        val = part[eq + 1:].strip()
        result[key] = val
    return result


def parse_trains_from_html(html_text):
    """HTML 응답에서 setSchedule() 호출을 파싱하여 열차 리스트 추출."""
    # HTML 엔티티 디코딩 (&#39; → ')
    decoded = html.unescape(html_text)

    # setSchedule('{...}', '1') 에서 일반실('1'), 특실('2') 데이터 추출
    pattern = r"setSchedule\('(\{[^}]+\})'\s*,\s*'[12]'\)"
    matches = re.findall(pattern, decoded)

    if not matches:
        return None

    # trnNo+dptTm 기준으로 중복 제거 (같은 열차가 특실/일반실/결합상품에 반복)
    seen = set()
    trains = []
    for m in matches:
        data = _parse_java_map(m)
        key = data.get("trnNo", "") + "_" + data.get("dptTm", "")
        if key in seen:
            continue
        seen.add(key)
        trains.append(data)

    return trains


def fmt_time(tm):
    """'060000' -> '06:00' 형식 변환."""
    if not tm or len(str(tm)) < 4:
        return str(tm) if tm else "?"
    s = str(tm)
    return f"{s[:2]}:{s[2:4]}"


def is_seat_available(status_str):
    """좌석 상태 문자열로 예매 가능 여부 판단."""
    s = str(status_str).strip()
    return "매진" not in s and s not in SOLD_OUT_KEYWORDS


def display_trains(trains, dpt_name, arv_name, date_str):
    """열차 리스트를 표 형태로 출력. 파싱된 리스트 반환."""
    parsed = []

    if not trains:
        print("⚠️  열차 리스트를 파싱할 수 없습니다. --inspect 옵션으로 원본 응답을 확인해주세요.")
        return parsed

    dt = datetime.strptime(date_str, "%Y%m%d")
    print(f"\n  🚄 {dpt_name} → {arv_name}  |  {dt.strftime('%Y.%m.%d')} {WEEKDAYS_KR[dt.weekday()]}")
    print(f"  {'─' * 68}")
    print(f"  {'번호':>4}  {'열차':>6}  {'출발':>6}  {'도착':>6}  {'소요':>6}  {'일반실':^10}  {'특실':^10}")
    print(f"  {'─' * 68}")

    GREEN = "\033[92m"
    RED = "\033[91m"
    RESET = "\033[0m"

    for i, item in enumerate(trains):
        trn_no = item.get("trnNo", "?").lstrip("0") or "?"
        dpt_tm = item.get("dptTm", "")
        arv_tm = item.get("arvTm", "")
        gnrm = item.get("gnrmRsvPsbCdNm", "?")
        sprm = item.get("sprmRsvPsbCdNm", "?")
        duration = item.get("timeDuration", "")

        gnrm_avail = is_seat_available(gnrm)
        sprm_avail = is_seat_available(sprm)

        gnrm_label = "예약가능" if gnrm_avail else "매진"
        sprm_label = "예약가능" if sprm_avail else "매진"

        gnrm_disp = f"{GREEN}{gnrm_label}{RESET}" if gnrm_avail else f"{RED}{gnrm_label}{RESET}"
        sprm_disp = f"{GREEN}{sprm_label}{RESET}" if sprm_avail else f"{RED}{sprm_label}{RESET}"

        print(f"  {i + 1:>4}   {trn_no:>6}   {fmt_time(dpt_tm):>5}   {fmt_time(arv_tm):>5}   {duration:>6}  {gnrm_disp:^19}  {sprm_disp:^19}")

        parsed.append({
            "index": i,
            "trainNo": trn_no,
            "dptTm": dpt_tm,
            "arvTm": arv_tm,
            "gnrm": gnrm,
            "sprm": sprm,
            "gnrm_avail": gnrm_avail,
            "sprm_avail": sprm_avail,
            "raw": item,
        })

    print(f"  {'─' * 68}\n")
    return parsed


# ─── 알림 ──────────────────────────────────────────────────────

def notify_macos(title, message):
    """macOS 알림 센터를 통해 알림 전송."""
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{message}" with title "{title}" sound name "Glass"'
        ], check=False)
    except Exception:
        pass
    # 터미널 벨 + 출력
    print(f"\a🔔 {title}: {message}")


# ─── 메인 로직 ─────────────────────────────────────────────────

def select_trains(parsed):
    """사용자가 감시할 열차를 선택."""
    print("  감시할 열차 번호를 입력하세요 (쉼표로 구분, 예: 1,3,5)")
    print("  전체 감시: all  |  종료: q")

    while True:
        try:
            sel = input("  > ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n종료합니다.")
            sys.exit(0)

        if sel == "q":
            sys.exit(0)
        if sel == "all":
            return list(range(len(parsed)))

        try:
            indices = [int(x.strip()) - 1 for x in sel.split(",")]
            if all(0 <= idx < len(parsed) for idx in indices):
                return indices
            print(f"  1~{len(parsed)} 범위의 번호를 입력해주세요.")
        except ValueError:
            print("  숫자를 입력해주세요.")


def monitor_loop(dpt_name, arv_name, date_str, watch_indices, parsed,
                 interval, passengers=None):
    """선택한 열차를 주기적으로 폴링하며 빈자리 감시."""
    watch_train_nos = {parsed[i]["trainNo"] for i in watch_indices}
    watch_dpt_tms = {parsed[i]["dptTm"] for i in watch_indices}

    print(f"  🔍 감시 시작 | {len(watch_indices)}개 열차 | 간격 {interval}초")
    print(f"  감시 대상: ", end="")
    for i in watch_indices:
        t = parsed[i]
        print(f"{t['trainNo']}({fmt_time(t['dptTm'])})", end="  ")
    print(f"\n  Ctrl+C로 종료\n")

    check_count = 0
    found_available = set()  # 이미 알림 보낸 열차 (중복 알림 방지)

    while True:
        try:
            check_count += 1
            now = datetime.now().strftime("%H:%M:%S")
            print(f"  [{now}] #{check_count} 조회 중...", end="", flush=True)

            try:
                html_text = fetch_schedule(dpt_name, arv_name, date_str, passengers)
            except requests.RequestException as e:
                print(f" 네트워크 오류: {e}")
                time.sleep(interval)
                continue

            trains = parse_trains_from_html(html_text)
            if not trains:
                print(" 파싱 실패")
                time.sleep(interval)
                continue

            # 감시 대상 열차의 좌석 상태 확인
            new_available = []
            status_parts = []

            for item in trains:
                train_no = item.get("trnNo", "").lstrip("0") or ""
                dpt_tm = item.get("dptTm", "")

                if train_no not in watch_train_nos and dpt_tm not in watch_dpt_tms:
                    continue

                gnrm = item.get("gnrmRsvPsbCdNm", "")
                sprm = item.get("sprmRsvPsbCdNm", "")
                gnrm_ok = is_seat_available(gnrm)
                sprm_ok = is_seat_available(sprm)

                key = f"{train_no}_{dpt_tm}"
                status = "매진" if not (gnrm_ok or sprm_ok) else ""
                parts = []
                if gnrm_ok:
                    parts.append("일반:가능")
                if sprm_ok:
                    parts.append("특실:가능")
                if parts:
                    status = "/".join(parts)

                status_parts.append(f"{train_no}({fmt_time(dpt_tm)})={status}")

                if (gnrm_ok or sprm_ok) and key not in found_available:
                    found_available.add(key)
                    seat_type = []
                    if gnrm_ok:
                        seat_type.append("일반실")
                    if sprm_ok:
                        seat_type.append("특실")
                    new_available.append({
                        "trainNo": train_no,
                        "dptTm": dpt_tm,
                        "seats": ", ".join(seat_type),
                    })

                # 이전에 가능했다가 다시 매진된 경우 알림 리셋
                if not (gnrm_ok or sprm_ok) and key in found_available:
                    found_available.discard(key)

            print(f" {' | '.join(status_parts)}")

            # 빈자리 발견 시 알림
            for av in new_available:
                msg = f"열차 {av['trainNo']} {fmt_time(av['dptTm'])} - {av['seats']}"
                notify_macos("🚄 SRT 빈자리 발견!", msg)

            time.sleep(interval)

        except KeyboardInterrupt:
            print(f"\n\n  ✋ 감시 종료 (총 {check_count}회 조회)")
            break


def main():
    parser = argparse.ArgumentParser(
        description="SRT 빈자리 모니터 - SRTplay 기반",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
사용 예시:
  python srt_monitor.py --from 수서 --to 부산 --date 20260418
  python srt_monitor.py --from 수서 --to 평택지제 --date 20260418 --interval 30
  python srt_monitor.py --from 수서 --to 부산 --date 20260418 --inspect
  python srt_monitor.py --stations  # 역 목록 확인
        """,
    )
    parser.add_argument("--from", dest="dpt", default="수서", help="출발역 (기본: 수서)")
    parser.add_argument("--to", dest="arv", default="평택지제", help="도착역 (기본: 평택지제)")
    parser.add_argument("--date", default=None, help="출발일 (YYYYMMDD, 예: 20260418)")
    parser.add_argument("--interval", type=int, default=30,
                        help="폴링 간격 초 (기본: 30)")
    parser.add_argument("--passengers", default="1,0,0,0,0",
                        help="승객 수 (어른,어린이,경로,장애,유아) 기본: 1,0,0,0,0")
    parser.add_argument("--inspect", action="store_true",
                        help="API 응답 원본을 JSON으로 출력하고 종료")
    parser.add_argument("--stations", action="store_true",
                        help="사용 가능한 역 목록 출력")

    args = parser.parse_args()

    if args.stations:
        print("\n사용 가능한 역:")
        for name, code in STATIONS.items():
            print(f"  {name} ({code})")
        sys.exit(0)

    if not args.date:
        parser.error("--date는 필수입니다 (예: --date 20260418)")

    passengers = [int(x) for x in args.passengers.split(",")]
    if len(passengers) != 5:
        print("❌ --passengers는 5개 값이어야 합니다 (예: 1,0,0,0,0)")
        sys.exit(1)

    # 1) 스케줄 조회
    print(f"\n  🔎 {args.dpt} → {args.arv} ({args.date}) 열차 조회 중...")
    html_text = fetch_schedule(args.dpt, args.arv, args.date, passengers)

    # inspect 모드: 원본 HTML 출력
    if args.inspect:
        print(html_text)
        sys.exit(0)

    # 2) 열차 목록 표시
    trains = parse_trains_from_html(html_text)
    parsed = display_trains(trains, args.dpt, args.arv, args.date)

    if not parsed:
        print("열차 정보를 가져올 수 없습니다. --inspect로 응답을 확인해주세요.")
        sys.exit(1)

    # 3) 감시 대상 선택
    watch_indices = select_trains(parsed)

    # 4) 폴링 시작
    monitor_loop(args.dpt, args.arv, args.date, watch_indices, parsed,
                 args.interval, passengers)


if __name__ == "__main__":
    main()
