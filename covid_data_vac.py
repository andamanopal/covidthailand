import datetime
from dateutil.parser import parse as d
import json
import os
import re
import copy
import numpy as np
import pandas as pd
import requests

from utils_pandas import daily2cum, export, import_csv
from utils_scraping import MAX_DAYS, USE_CACHE_DATA, any_in, get_next_number, get_next_numbers, \
    pairwise, parse_file, parse_numbers, replace_matcher, split, \
    web_files, web_links, NUM_OR_DASH, logger, camelot_cache
from utils_thai import area_crosstab, find_thai_date, get_province, join_provinces, today


################################
# Vaccination reports
################################

def get_vaccination_coldchain(request_json, join_prov=False):
    logger.info("Requesting coldchain: {}", request_json)
    if join_prov:
        df_codes = pd.read_html("https://en.wikipedia.org/wiki/ISO_3166-2:TH")[0]
        codes = [code for code, prov, ptype in df_codes.itertuples(index=False) if "special" not in ptype]
        provinces = [
            prov.split("(")[0] for code, prov, ptype in df_codes.itertuples(index=False) if "special" not in ptype
        ]
        provinces = [get_province(prov) for prov in provinces]
    else:
        provinces = codes = [None]

    url = "https://datastudio.google.com/batchedDataV2?appVersion=20210506_00020034"
    with open(request_json) as fp:
        post = json.load(fp)
    specs = post['dataRequest']
    post['dataRequest'] = []

    def set_filter(filters, field, value):
        for filter in filters:
            if filter['filterDefinition']['filterExpression']['queryTimeTransformation']['dataTransformation'][
                    'sourceFieldName'] == field:
                filter['filterDefinition']['filterExpression']['stringValues'] = value
        return filters

    def make_request(post, codes):
        for code in codes:
            for spec in specs:
                pspec = copy.deepcopy(spec)
                if code:
                    set_filter(pspec['datasetSpec']['filters'], "_hospital_province_code_", [code])
                post['dataRequest'].append(pspec)
        try:
            r = requests.post(url, json=post, timeout=120)
        except requests.exceptions.ReadTimeout:
            logger.info("Timeout so using cached {}", request_json)
            with open(os.path.join("inputs", "json", request_json), ) as fp:
                data = fp.read()
        else:
            _, _, data = r.text.split("\n")
        data = json.loads(data)
        if any(resp for resp in data['dataResponse'] if 'errorStatus' in resp):
            # raise Exception(resp['errorStatus']['reasonStr'])
            # read from cache if possible
            with open(os.path.join("inputs", "json", request_json)) as fp:
                data = json.load(fp)
        else:
            with open(os.path.join("inputs", "json", request_json), "w") as fp:
                fp.write(data)
        for resp in (resp for resp in data['dataResponse'] if 'errorStatus' in resp):
            # raise Exception(resp['errorStatus']['reasonStr'])
            pass
        for resp in (resp for resp in data['dataResponse'] if 'errorStatus' not in resp):
            yield resp
    if join_prov:
        dfall = pd.DataFrame(columns=["Date", "Province", "Vaccine"]).set_index(["Date", "Province", "Vaccine"])
    else:
        dfall = pd.DataFrame(columns=["Date"]).set_index(["Date"])

    for prov_spec, data in zip([(p, s) for p in provinces for s in specs], make_request(post, codes)):
        prov, spec = prov_spec
        fields = [(f['name'], f['dataTransformation']['sourceFieldName']) for f in spec['datasetSpec']['queryFields']]
        for datasubset in data['dataSubset']:
            colmuns = datasubset['dataset']['tableDataset']['column']
            df_cols = {}
            date_col = None
            for field, column in zip(fields, colmuns):
                fieldname = dict(_vaccinated_on_='Date',
                                _manuf_name_='Vaccine',
                                datastudio_record_count_system_field_id_98323387='Vac Given').get(field[1], field[1])
                nullIndex = column['nullIndex']
                del column['nullIndex']
                if column:
                    field_type = next(iter(column.keys()))
                    conv = dict(dateColumn=d, datetimeColumn=d, longColumn=int, doubleColumn=float,
                                stringColumn=str)[field_type]
                    values = [conv(i) for i in column[field_type]['values']]
                    if conv == d:
                        date_col = fieldname
                else:
                    values = []
                # datastudio_record_count_system_field_id_98323387 = supply?
                for i in nullIndex:  # TODO check we are doing this right
                    values.insert(i, None)
                df_cols[fieldname] = values
            df = pd.DataFrame(df_cols)
            if not date_col:
                df['Date'] = today()
            else:
                df['Date'] = df[date_col]
            if prov:
                df['Province'] = prov
                df = df.set_index(["Date", "Province", "Vaccine"])
            else:
                df = df.set_index(['Date'])
            dfall = dfall.combine_first(df)
    return dfall

# vac given table
# <p>สรุปการฉดีวัคซีนโควิด 19 ตัง้แตว่ันที่ 7 มิถุนายน 2564
# ผลการใหบ้ริการ ณ วนัที ่23 มิถุนายน 2564 เวลา 18.00 น.


def vac_problem(daily, date, file, page):
    if "Anaphylaxis" not in page:
        return daily
    prob_a, rest = get_next_number(page, "Anaphylaxis")
    prob_p, rest = get_next_number(page, "Polyneuropathy")
    row = [date, prob_a, prob_p]
    assert not any_in(['None'], row)
    df = pd.DataFrame([row], columns=[
        "Date",
        "Vac Problem Anaphylaxis",
        "Vac Problem Polyneuropathy",
    ]).set_index("Date")
    return daily.combine_first(df)


def vaccination_daily(daily, date, file, page):
    if not re.search(r"(ให้หน่วยบริกำร|ใหห้นว่ยบริกำร|สรปุกำรจดัสรรวคัซนีโควดิ 19|ริการวัคซีนโควิด 19|ผู้ได้รับวัคซีนเข็มที่ 1)", page):  # noqa
        return daily
    date = find_thai_date(page)
    # fix numbers with spaces in them
    page = re.sub(r"(\d) (,\d)", r"\1\2", page)
    if date == d("2021-05-06"):
        page = re.sub(r",5 9 รำย", "", page)  # glitch on 2021-05-6
    # dose1_total, rest1 = get_next_number(page, "ได้รับวัคซีนเข็มที่ 1", until="โดส")
    # dose2_total, rest2 = get_next_number(page, "ได้รับวัคซีน 2 เข็ม", until="โดส")

    alloc_sv, rest = get_next_number(page, "Sinovac", until="โดส")
    alloc_az, rest = get_next_number(page, "AstraZeneca", until="โดส")

    # numbers, _ = get_next_numbers(page, "2 (รำย) รวม (โดส)")
    # if numbers:
    #     given1, given2, given_total, *_ = numbers

    # alloc_total, rest = get_next_number(page, "รวมกำรจัดสรรวัคซีนทั้งหมด", "รวมกำรจดัสรรวคัซนีทัง้หมด", until="โดส")
    # assert alloc_total == alloc_sv + alloc_az
    row = [date, alloc_sv, alloc_az]
    # assert not any_in(row, None)
    df = pd.DataFrame([row], columns=[
        "Date",
        "Vac Allocated Sinovac",
        "Vac Allocated AstraZeneca",
    ]).set_index("Date").fillna(value=np.nan)
    # TODO: until make more specific to only reports for allocations
    daily = daily.combine_first(df)

    if not re.search(r"(ากรทางการแพท|บุคคลที่มีโรคประจ|ากรทางการแพทย|กรทำงกำรแพทย์)", page):
        logger.info("{} Vac Sum (Missing groups) {} {}", date.date(), df.to_string(header=False, index=False), file)
        assert date < d("2021-07-12")
        return daily

    def clean_num(numbers):
        if len(numbers) > 8:
            return [n for n in numbers if n not in (60, 17, 12, 7, 3)]
        else:
            return [n for n in numbers if n not in (60, 7)]

    page = re.sub("ผัสผู้ป่วย 1,022", "", page)  # 2021-05-06

    # Daily totals at the bottom often make it harder to get the right numbers
    # ส ำหรับรำยงำนจ ำนวนผู้ได้รับวัคซีนโควิด 19 เพิ่มขึ้นในวันที่ 17 ตุลำคม 2564 มีผู้ได้รับวัคซีนทั้งหมด
    gtext, *_ = re.split("หรับรำยงำนจ", page)

    d1_num, rest1 = get_next_numbers(gtext,
                                    r"1\s*(?:จํานวน|จำนวน|จ ำนวน)",
                                    r"เข็ม(?:ท่ี|ที่) 1 จํานวน",
                                    r"ซีนเข็มที่ 1 จ",
                                    until=r"(?:2 เข็ม)", return_until=True, require_until=True)
    d2_num, rest2 = get_next_numbers(gtext,
                                    r"ได้รับวัคซีน 2 เข็ม",
                                    r"ไดรับวัคซีน 2 เข็ม",
                                    until=r"(?:ดังรูป|โควิด 19|จังหวัดที่|\(Booster dose\))", return_until=True, require_until=True)
    d3_num, rest3 = get_next_numbers(gtext, r"\(Booster dose\)", until="ดังรูป", return_until=True)
    if not len(clean_num(d1_num)) == len(clean_num(d2_num)):
        if date > d("2021-04-24"):
            ld1, ld2 = len(clean_num(d1_num)), len(clean_num(d2_num))
            error = f"ERROR number of first doses ({ld1}) does not equal number of second doses ({ld2}) in {file} for {date}",
            logger.error(error)
            assert False, error
        else:
            logger.info("{} Vac Sum (Error groups) {} {}", date.date(), df.to_string(header=False, index=False), file)
            return daily
    # assert len(d3_num) == 0 or len(d3_num) == len(d2_num)

    is_risks = re.compile(r"(บุคคลที่มีโรคประจ|บุคคลท่ีมีโรคประจําตัว|ผู้ที่มีอายุตั้งแต่ 60|จำนวน|ได้รับวัคซีน 2|7 กลุ)")

    for dose, numbers, rest in [(1, d1_num, rest1), (2, d2_num, rest2), (3, d3_num, rest3)]:
        cols = [
            "Date",
            f"Vac Given {dose} Cum",
            f"Vac Group Medical All {dose} Cum",
            f"Vac Group Medical Staff {dose} Cum",
            f"Vac Group Health Volunteer {dose} Cum",
            f"Vac Group Other Frontline Staff {dose} Cum",
            f"Vac Group Over 60 {dose} Cum",
            f"Vac Group Risk: Disease {dose} Cum",
            f"Vac Group Risk: Pregnant {dose} Cum",
            f"Vac Group Risk: Location {dose} Cum",
            f"Vac Group Student {dose} Cum",
        ]
        numbers = clean_num(numbers)  # remove 7 chronic diseases and over 60 from numbers
        if (num_len := len(numbers)) in (6, 8, 9) and is_risks.search(rest):
            if num_len >= 8:
                # They changed around the order too much. have to switch to picking per category
                total, *_ = numbers
                medical = get_next_number(rest, r"างการแพท", r"งกำรแพท", until="(?:ราย|รำย)", return_rest=False, thainorm=True, asserted=True)
                frontline = get_next_number(rest, r"นหน้ำ", r"านหน้า", r"านหนา", until="(?:ราย|รำย)", return_rest=False, thainorm=True, asserted=False)
                volunteer = get_next_number(rest, r"อาสาสมัคร", r"อำสำสมัคร", until="(?:ราย|รำย)", return_rest=False, thainorm=True, asserted=True)
                over60 = get_next_number(rest, r"60 *(?:ปี|ป)\s*?\s*(?:ขึ|ปี|ข้ึ)", until="(?:ราย|รำย)", return_rest=False, asserted=True)
                d7, chronic, *_ = get_next_numbers(rest, r"โรค", until="(?:ราย|รำย)", return_rest=False, thainorm=True, asserted=True)
                assert d7 == 7
                pregnant = get_next_number(rest, r"งครร(?:ภ์|ภ)", r"จำนวน", until="(?:ราย|รำย)", return_rest=False, thainorm=True, asserted=True)
                area = get_next_number(rest, r"าชนทั่วไป", r"ประชาชน", r"ประชำชน", until="(?:ราย|รำย)", return_rest=False, thainorm=True, asserted=True)
                student = get_next_numbers(rest, r"นักเรียน", until="(?:ราย|รำย)", return_rest=False, thainorm=True, asserted=False)
                if len(student) == 3:
                    d12, d17, student = student
                    assert (d12, d17) == (12, 17)
                elif len(student) == 0:
                    student = np.nan
                else:
                    # if something was captured into *student then hope it was the addition of students on 2021-10-06 or else...
                    raise Exception("Unexpected excess vaccination values found on {} in {}: {}", date, file, student)
                med_all = medical + volunteer
                if date in [d("2021-08-11")] and dose == 2:
                    frontline = None  # Wrong value for dose2
            else:
                total, med_all, frontline, over60, chronic, area = numbers
                pregnant = volunteer = medical = student = None
            row = [medical, volunteer, frontline, over60, chronic, pregnant, area, student]
            if date not in [d("2021-08-11")]:
                assert not any_in([None, np.nan], medical or med_all, over60, chronic, area)
                total_row = [medical or med_all, volunteer, frontline, over60, chronic, pregnant, area, student]
                assert 0.945 <= (sum(i for i in total_row if i and not pd.isna(i)) / total) <= 1.01
            df = pd.DataFrame([[date, total, med_all] + row], columns=cols).set_index("Date")
        elif dose == 3:
            if len(numbers) == 2:
                numbers = numbers + [np.nan] * 8
            elif len(numbers) == 0:
                numbers = [np.nan] * 10
            df = pd.DataFrame([[date] + numbers], columns=cols).set_index("Date")
        elif numbers:
            assert date < d("2021-07-12")  # Should be getting all the numbers every day now
            total, *_ = numbers
            df = pd.DataFrame([[date, total]], columns=[
                "Date",
                f"Vac Given {dose} Cum",
            ]).set_index("Date")
        else:
            assert date < d("2021-07-12")  # Should be getting all the numbers every day now
            continue
        daily = daily.combine_first(df)
    daily = daily.fillna(value=np.nan)
    logger.info("{} Vac Sum {} {}", date.date(), daily.loc[date:date].to_string(header=False, index=False), file)
    return daily


def vaccination_tables(df, date, page, file):
    date = find_thai_date(page)
    givencols = [
        "Date",
        "Province",
        "Vac Given 1 Cum",
        "Vac Given 1 %",
        "Vac Given 2 Cum",
        "Vac Given 2 %",
    ]
    givencols3 = givencols + [
        "Vac Given 3 Cum",
        "Vac Given 3 %",
    ]
    vaccols8x3 = givencols3 + [
        f"Vac Group {g} {d} Cum" for g in [
            "Medical Staff", "Health Volunteer", "Other Frontline Staff", "Over 60", "Risk: Disease", "Risk: Pregnant",
            "Risk: Location", "Student"
        ] for d in range(1, 4)
    ]
    vaccols7x3 = [col for col in vaccols8x3 if "Student" not in col]  # Student vaccination figures did not exist prior to 2021-10-06
    vaccols6x2 = [col for col in vaccols7x3 if " 3 " not in col and "Pregnant" not in col]
    vaccols5x2 = [col for col in vaccols6x2 if "Volunteer" not in col]

    alloc2_doses = [
        "Date",
        "Province",
        "Vac Allocated Sinovac 1",
        "Vac Allocated Sinovac 2",
        "Vac Allocated AstraZeneca 1",
        "Vac Allocated AstraZeneca 2",
        "Vac Allocated Sinovac",
        "Vac Allocated AstraZeneca",
    ]
    alloc2 = [
        "Date",
        "Province",
        "Vac Allocated Sinovac",
        "Vac Allocated AstraZeneca",
    ]
    alloc4 = alloc2 + ["Vac Allocated Sinopharm", "Vac Allocated Pfizer", "Vac Allocated Moderna",]

    # def add(df, prov, numbers, cols):
    #     if not df.empty:
    #         try:
    #             prev = df[cols].loc[[date, prov]]
    #         except KeyError:
    #             prev = None
    #         msg = f"Vac {date} {prov} repeated: {numbers} != {prev}"
    #         assert prev in [None, numbers], msg
    #     row = [date, prov] + numbers
    #     df = df.combine_first(pd.DataFrame([row], columns=cols).set_index(["Date", "Province"]))
    #     return df

    rows = {}

    def add(prov, numbers, cols):
        assert rows.get((date, prov), None) is None or rows.get((date, prov), None).keys() != cols
        rows[(date, prov)] = {c: n for c, n in zip(cols, [date, prov] + numbers)} | rows.get((date, prov), {})

    shots = re.compile(r"(เข็ม(?:ที|ที่|ท่ี)\s.?(?:1|2)\s*)")
    july = re.compile(r"\( *(?:ร้อยละ|รอ้ยละ) *\)", re.DOTALL)
    oldhead = re.compile(r"(เข็มที่ 1 วัคซีน|เข็มท่ี 1 และ|เข็มที ่1 และ)")

    def in_heading(pat):
        return max(len(pat.findall(h)) for h in headings)
    lines = [line.strip() for line in page.split('\n') if line.strip()]
    _, *rest = split(lines, lambda x: (july.search(x) or shots.search(x) or oldhead.search(x)) and '2564' not in x)
    for headings, lines in pairwise(rest):
        shot_count = in_heading(shots)
        table = {12: "new_given", 10: "given", 6: "alloc", 14: "july", 16: "july"}.get(shot_count)
        if not table and in_heading(oldhead):
            table = "old_given"
        elif not table and in_heading(july) and in_heading(re.compile(r"(?:ร้อยละ|รอ้ยละ)")) and date > d("2021-08-01"):  # new % table
            table = "percent"
        elif not table and in_heading(july):
            table = "july"
        elif not table:
            continue
        added = None
        for line in lines:
            # fix some number broken in the middle
            line = re.sub(r"(\d+ ,\d+)", lambda x: x.group(0).replace(" ", ""), line)
            area, *rest = line.split(' ', 1)
            if area in ["เข็มที่", "และ", "จ", "ควำมครอบคลุม", 'ตั้งแต่วันที่', 'หมายเหตุ', 'เขต', 'เข็ม']:  # Extra heading
                continue
            if area == "รวม" or not rest:
                continue  # previously meant end of table. Now can be part of header. 2021-08-14
            cols = [c.strip() for c in NUM_OR_DASH.split(rest[0]) if c.strip()]
            if len(cols) < 5:
                break
            if added is None:
                added = 0
            if NUM_OR_DASH.match(area):
                thaiprov, *cols = cols
            else:
                thaiprov = area
            prov = get_province(thaiprov)
            numbers = parse_numbers(cols)
            added += 1
            if table == "alloc":
                sv1, sv2, az1, az2 = numbers[3:7]
                add(prov, [sv1, sv2, az1, az2, sv1 + sv2, az1 + az2], alloc2_doses)
            elif table == "given":
                if len(numbers) == 16:
                    alloc_sv, alloc_az, *numbers = numbers
                    add(prov, [alloc_sv, alloc_az], alloc2)
                assert len(numbers) == 14
                add(prov, numbers, vaccols5x2)
            elif table == "old_given":
                alloc, target_num, given, perc, *rest = numbers
                medical, frontline, disease, elders, riskarea, *rest = rest
                # TODO: #อยู่ระหว่ำง ระบุ กลุ่มเป้ำหมำย - In the process of specifying the target group
                # unknown = sum(rest)
                row = [given, perc, 0, 0] + [medical, 0, frontline, 0, disease, 0, elders, 0, riskarea, 0]
                add(prov, row, vaccols5x2)
                add(prov, [alloc, 0, 0, 0, alloc, 0], alloc2_doses)
            elif table == "new_given" and len(numbers) == 12:  # e.g. vaccinations/Daily report 2021-05-11.pdf
                dose1, dose2, *groups = numbers
                add(prov, [dose1, np.nan, dose2, np.nan] + groups, vaccols5x2)
            elif table == "new_given" and len(numbers) == 21:  # from 2021-07-20
                # Actually cumulative totals
                pop, alloc, givens, groups = numbers[0], numbers[1:4], numbers[4:8], numbers[9:21]
                sv, az, total_alloc = alloc
                add(prov, givens + groups + [pop], vaccols6x2 + ["Vac Population"])
                add(prov, [sv, az], alloc2)
            elif table == "july" and len(numbers) == 5:
                pop, given1, perc1, given2, perc2, = numbers
                row = [given1, perc1, given2, perc2]
                add(prov, row, givencols)
            elif table == "july" and len(numbers) in [31, 32, 33, 27, 21, 22, 17]:  # from 2021-08-05
                # Actually cumulative totals
                if len(numbers) == 21:
                    # Givens is a single total only 2021-08-16
                    pop, alloc, givens, groups = numbers[0], numbers[1:5], numbers[5:6], numbers[6:]
                    givens = [np.nan] * 6  # We don't use the total
                elif len(numbers) == 22 and date < datetime.datetime(2021, 10, 5):
                    # Givens has sinopharm in it too. 2021-08-15
                    pop, alloc, givens, groups = numbers[0], numbers[1:6], numbers[6:7], numbers[7:]
                    givens = [np.nan] * 6  # We don't use the total
                elif len(numbers) == 17:
                    # No allocations or givens 2021-08-10
                    pop, givens, groups = numbers[0], numbers[1:2], numbers[2:]
                    givens = [np.nan] * 6
                    alloc = [np.nan] * 4
                elif len(numbers) in [27, 33]:  # 2021-08-06, # 2021-08-05
                    pop, alloc, givens, groups = numbers[0], numbers[1:5], numbers[5:11], numbers[12:]
                elif len(numbers) == 31:  # 2021-10-05
                    pop, alloc, groups = numbers[0], numbers[1:6], numbers[7:]
                    # TODO: put in manuf given per province?
                    givens = [np.nan] * 6
                elif len(numbers) == 32:  # 2021-11-17
                    pop, alloc, groups = numbers[0], numbers[1:7], numbers[8:]
                    # TODO: put in manuf given per province?
                    givens = [np.nan] * 6
                else:
                    assert False
                sp = md = np.nan
                if len(alloc) == 4:  # 2021-08-06
                    sv, az, pf, total_alloc = alloc
                elif len(alloc) == 5:  # 2021-08-15
                    sv, az, sp, pf, total_alloc = alloc
                elif len(alloc) == 6:  # 2021-08-15
                    sv, az, sp, pf, md, total_alloc = alloc
                else:
                    assert False
                assert pd.isna(total_alloc) or sum([m for m in [sv, az, pf, sp, md] if not pd.isna(m)]) == total_alloc
                if len(groups) == 15:  # 2021-08-06
                    # medical has 3 doses, rest 2, so insert some Nones
                    for i in range(5, len(groups) + 6, 3):
                        groups.insert(i, np.nan)
                if len(groups) < 24:
                    groups = groups + [np.nan] * 3  # students
                add(prov, givens + groups + [pop], vaccols8x3 + ["Vac Population"])
                add(prov, [sv, az, sp, pf, md], alloc4)
            elif table == "percent" and len(numbers) in [13]:  # 2021-08-10
                # extra table with %  per population for over 60s and totals
                pop, d1, d1p, d2, d2p, d3, d3p, total, pop60, d60_1, d60_1p, d60_2, d60_2p = numbers
                add(prov, [d1, d1p, d2, d2p, d3, d3p], givencols3)
            elif table == "percent" and len(numbers) in [18, 22]:
                # extra table with %  per population for over 60s and totals - 2021-09-09, 2021-10-05
                pop, d1, d1p, d2, d2p, d3, d3p, *_ = numbers
                add(prov, [d1, d1p, d2, d2p, d3, d3p], givencols3)
            else:
                assert False, f"No vac table format match for {len(numbers)} cols in {file} {str(date)}"
        assert added is None or added > 7
    rows = pd.DataFrame.from_dict(rows, orient='index')
    rows = rows.set_index(["Date", "Province"]).fillna(np.nan) if not rows.empty else rows
    percents = rows[list(rows.columns.intersection([f'Vac Given {i} %' for i in range(1, 5)]))].fillna(0)
    assert (percents < 500).all().all(), f"{file} {date}: wrong allocations"
    # if 'Vac Given 1 Cum' in rows.columns:
    #     rows['Vac Given Cum'] = rows[list(rows.columns.intersection([f'Vac Given {i} Cum' for i in range(1, 5)]))].sum(axis=1)
    return df.combine_first(rows) if not rows.empty else df


# def vaccination_reports_files():
#     # also from https://ddc.moph.go.th/vaccine-covid19/diaryReportMonth/08/9/2021
#     folders = web_links("https://ddc.moph.go.th/dcd/pagecontent.php?page=643&dept=dcd",
#                         ext=None, match=re.compile("2564"))
#     links = (link for f in folders for link in web_links(f, ext=".pdf"))
#     url = "https://ddc.moph.go.th/uploads/ckeditor2/files/Daily report "
#     gen_links = (f"{url}{f.year}-{f.month:02}-{f.day:02}.pdf"
#                  for f in reversed(list(daterange(d("2021-05-20"), today(), 1))))
#     links = unique_values(chain(links, gen_links))  # Some were not listed on the site so we guess
#     links = sorted(links, key=lambda f: date if (date := file2date(f)) is not None else d("2020-01-01"), reverse=True)
#     for link in links:
#         date = file2date(link)
#         if not date or date <= d("2021-02-27"):
#             continue
#         date = date - datetime.timedelta(days=1)  # TODO: get actual date from titles. maybe not always be 1 day delay
#         if USE_CACHE_DATA and date < today() - datetime.timedelta(days=MAX_DAYS - 1):
#             break

#         def get_file(link=link):
#             try:
#                 file, _, _ = next(iter(web_files(link, dir="vaccinations")))
#             except StopIteration:
#                 return None
#             return file

#         yield link, date, get_file


def vaccination_reports_files2(check=True):
    # https://ddc.moph.go.th/vaccine-covid19/diaryReport
    # or https://ddc.moph.go.th/dcd/pagecontent.php?page=643&dept=dcd
    folders = [f"https://ddc.moph.go.th/vaccine-covid19/diaryReportMonth/{m:02}/9/2021" for m in range(3, 13)]

    links = (link for f in folders for link in web_links(f, ext=".pdf", check=check))
    # links = sorted(links, reverse=True)
    links = reversed(list(links))
    count = 0
    for link in links:

        def get_file(link=link):
            try:
                file, _, _ = next(iter(web_files(link, dir="inputs/vaccinations")))
            except StopIteration:
                return None
            return file
        count += 1
        if USE_CACHE_DATA and count > MAX_DAYS:
            break
        yield link, None, get_file


def vaccination_reports():
    vac_daily = pd.DataFrame(columns=['Date']).set_index("Date")
    vac_prov_reports = pd.DataFrame(columns=['Date', 'Province']).set_index(["Date", "Province"])

    # add in newer https://ddc.moph.go.th/uploads/ckeditor2//files/Daily%20report%202021-06-04.pdf
    # Just need the latest

    for link, date, dl in vaccination_reports_files2():
        if (file := dl()) is None:
            continue
        table = pd.DataFrame(columns=["Date", "Province"]).set_index(["Date", "Province"])
        for page in parse_file(file):
            found_date = find_thai_date(page)
            if date is None:
                date = found_date
            table = vaccination_tables(table, date, page, file)

            vac_daily = vaccination_daily(vac_daily, date, file, page)
            vac_daily = vac_problem(vac_daily, date, file, page)
        logger.info("{} Vac Tables {} {} {}", date, len(table), "Provinces parsed", file)
        # TODO: move this into vaccination_tables so can be tested
        if d("2021-05-04") <= date <= d("2021-08-01") and len(table) < 77:
            logger.info("{} Dropping table: too few provinces", date)
            continue
        elif d("2021-04-09") <= date <= d("2021-05-03") and table.groupby("Date").count().iloc[0]['Vac Group Risk: Location 1 Cum'] != 77:
            #counts = table.groupby("Date").count()
            #missing_data = counts[counts['Vac Allocated AstraZeneca'] > counts['Vac Group Risk: Location 2 Cum']]
            # if not missing_data.empty:
            logger.info("{} Dropping table: alloc doesn't match prov", date)
            continue
        else:
            assert len(table) == 77 or date < d("2021-08-01")
        vac_prov_reports = vac_prov_reports.combine_first(table)

    # Do cross check we got the same number of allocations to vaccination
    # if not vac_prov_reports.empty:
    #     # counts = vac_prov_reports.groupby("Date").count()
    #     # missing_data = counts[counts['Vac Allocated AstraZeneca'] > counts['Vac Group Risk: Location 2 Cum']]
    #     # # 2021-04-08 2021-04-06 2021-04-05- 03-02 just not enough given yet
    #     # missing_data = missing_data["2021-04-09": "2021-05-03"]
    #     # # 2021-05-02 2021-05-01 - use images for just one table??
    #     # # We will just remove this days
    #     # vac_prov_reports = vac_prov_reports.drop(index=missing_data.index)
    #     # # After 2021-05-08 they stopped using allocation table. But cum should now always have 77 provinces
    #     # # TODO: only have 76 prov? something going on
    #     # missing_data = counts[counts['Vac Given 1 Cum'] < 77]["2021-05-04":]
    #     # vac_prov_reports = vac_prov_reports.drop(index=missing_data.index)

    #     # Just in case coldchain data not working

    return vac_daily, vac_prov_reports


def get_vac_coldchain():
    vac_import = get_vaccination_coldchain("vac_request_imports.json", join_prov=False)
    if not vac_import.empty:
        vac_import["_vaccine_name_"] = vac_import["_vaccine_name_"].apply(replace_matcher(["Astrazeneca", "Sinovac"]))
        vac_import = vac_import.drop(columns=['_arrive_at_transporter_']).pivot(columns="_vaccine_name_",
                                                                                values="_quantity_")
        vac_import.columns = [f"Vac Imported {c}" for c in vac_import.columns]
        vac_import = vac_import.fillna(0)
        vac_import['Vac Imported'] = vac_import.sum(axis=1)
        vac_import = vac_import.combine_first(daily2cum(vac_import))

    # Delivered Vac data from coldchain
    vac_delivered = get_vaccination_coldchain("vac_request_delivery.json", join_prov=False)
    vac_delivered = join_provinces(vac_delivered, '_hospital_province_')
    # TODO: save delivered by prov somewhere. note some hospitals unknown prov
    vac_delivered = vac_delivered.reset_index()
    vac_delivered['Date'] = vac_delivered['Date'].dt.floor('d')
    vac_delivered = vac_delivered[['Date', '_quantity_']].groupby('Date').sum()
    vac_delivered = vac_delivered.rename(columns=dict(_quantity_='Vac Delivered'))
    vac_delivered['Vac Delivered Cum'] = vac_delivered['Vac Delivered'].fillna(0).cumsum()

    # per prov given from coldchain
    vacct = get_vaccination_coldchain("vac_request_givenprov.json", join_prov=True)
    vacct = vacct.reset_index().set_index("Date").loc['2021-02-28':].reset_index().set_index(['Date', 'Province'])
    vacct = vacct.reset_index().pivot(index=["Date", "Province"], columns=["Vaccine"]).fillna(0)
    vacct.columns = [" ".join(c).replace("Sinovac Life Sciences", "Sinovac") for c in vacct.columns]
    vacct['Vac Given'] = vacct.sum(axis=1, skipna=False)
    vacct = vacct.loc[:today() - datetime.timedelta(days=1)]  # Today's data is incomplete
    vacct = vacct.fillna(0)
    vaccum = vacct.groupby(level="Province", as_index=False, group_keys=False).apply(daily2cum)
    vacct = vacct.combine_first(vaccum)

    # Their data can have some prov on the last day missing data
    # Need the last day we have a full set of data since some provinces can come in late in vac tracker data
    # TODO: could add unknowns
    counts1 = vacct['Vac Given'].groupby("Date").count()
    counts2 = vacct['Vac Given Cum'].groupby("Date").count()
    last_valid = max([counts2[counts1 > 76].last_valid_index(), counts2[counts2 > 76].last_valid_index()])
    vacct = vacct.loc[:last_valid]

    return vac_import, vac_delivered, vacct


def export_vaccinations(vac_reports, vac_reports_prov, vac_slides_data):
    # TODO: replace the vacct per prov data with the dashboard data
    # TODO: replace the import/delivered data with?
    # vac_import, vac_delivered, vacct = get_vac_coldchain()

    # vac_reports_prov.drop(columns=["Vac Given 1 %", "Vac Given 1 %"], inplace=True)

    # Not currently used as it is too likely to result in missing numbers
    # vac_prov_sum = vac_reports_prov.groupby("Date").sum()

    vac_prov = import_csv("vaccinations", ["Date", "Province"], not USE_CACHE_DATA)
    vac_prov = vac_prov.combine_first(vac_reports_prov)  # .combine_first(vacct)
    if not USE_CACHE_DATA:
        export(vac_prov, "vaccinations", csv_only=True)

    # vac_prov = vac_prov.combine_first(vacct)

    # Add totals if they are missing
    # given = vac_prov[[f"Vac Given {d}" for d in range(1, 4)]].sum(axis=1).to_frame("Vac Given")
    # vac_prov = vac_prov.combine_first(given)
    given_cum = vac_prov[[f"Vac Given {d} Cum" for d in range(1, 4)]].sum(axis=1).to_frame("Vac Given Cum")
    vac_prov = vac_prov.combine_first(given_cum)

    # Get vaccinations by district # TODO: move this to plot
    vac_prov = join_provinces(vac_prov, "Province")
    given_by_area_1 = area_crosstab(vac_prov, 'Vac Given 1', ' Cum')
    given_by_area_2 = area_crosstab(vac_prov, 'Vac Given 2', ' Cum')
    given_by_area_both = area_crosstab(vac_prov, 'Vac Given', ' Cum')

    vac_timeline = import_csv("vac_timeline", ["Date"], not USE_CACHE_DATA)

    vac_timeline = vac_timeline.combine_first(
        vac_reports).combine_first(
        vac_slides_data).combine_first(
        # vac_delivered).combine_first(
        # vac_import).combine_first(
        given_by_area_1).combine_first(
        given_by_area_2).combine_first(
        given_by_area_both)
    if not USE_CACHE_DATA:
        export(vac_timeline, "vac_timeline")

    return vac_timeline


def vac_manuf_given(df, page, file, page_num, url):
    if not re.search(r"(ผลการฉีดวคัซีนสะสมจ|ผลการฉีดวัคซีนสะสมจ|านวนผู้ได้รับวัคซีน|านวนการได้รับวัคซีนสะสม|านวนผูไ้ดร้บัวคัซนี)", page):  # noqa
        return df
    if "AstraZeneca" not in page or int(os.path.splitext(os.path.basename(file))[0]) <= 1620104912165:  # 2021-03-21
        return df
    table = camelot_cache(file, page_num, process_background=True)
    # should be just one col. sometimes there are extra empty ones. 2021-08-03
    table = table.replace('', np.nan).dropna(how="all", axis=1).replace(np.nan, '')
    title1, daily, title2, doses, *rest = [cell for cell in table[table.columns[0]] if cell.strip()]  # + title3, totals + extras
    date = find_thai_date(title1)
    # Sometimes header and cell are split into different rows 'vaccinations/1629345010875.pdf'
    if len(rest) == 3 and date < d("2021-10-14"):
        # TODO: need better way to detect this case
        doses = rest[0]  # Assumes header is doses cell

    # Sometimes there is an extra date thrown in inside brackets on the subheadings
    # e.g. vaccinations/1624968183817.pdf
    _, doses = find_thai_date(doses, remove=True)

    numbers = get_next_numbers(doses, return_rest=False)
    numbers = [n for n in numbers if n not in [1, 2, 3]]  # these are in subtitles and seem to switch positions
    sp1, sp2, sp3 = [0] * 3
    pf1, pf2, pf3 = [0] * 3
    az3, sv3, sp3 = [0] * 3
    mod1, mod2, mod3 = [0] * 3
    total3 = 0
    if "moderna" in doses.lower():
        total1, sv1, az1, sp1, pf1, mod1, total2, sv2, az2, sp2, pf2, mod2, total3, az3, pf3, mod3 = numbers
    elif "pfizer" in doses.lower():
        total1, sv1, az1, sp1, pf1, total2, sv2, az2, sp2, pf2, total3, *dose3 = numbers
        if len(dose3) == 2:
            az3, pf3 = dose3
        elif len(dose3) == 4:
            sv3, az3, sp3, pf3 = dose3
        else:
            assert False, f"wrong number of vac in {file}.{date}\n{page}"
    elif "Sinopharm" in doses:
        total1, sv1, az1, sp1, total2, sv2, az2, sp2 = numbers
    else:
        if len(numbers) == 6:
            total1, sv1, az1, total2, sv2, az2 = numbers
        else:
            # vaccinations/1620456296431.pdf # somehow ends up inside brackets
            total1, sv1, az1, sv2, az2 = numbers
            total2 = sv2 + az2
    assert total1 == sv1 + az1 + sp1 + pf1 + mod1
    #assert total2 == sv2 + az2 + sp2 + pf2
    # 1% tolerance added for error from vaccinations/1633686565437.pdf on 2021-10-06
    assert total3 == 0 or date in [d("2021-08-15")] or 0.99 <= total3 / (sv3 + az3 + sp3 + pf3 + mod3) <= 1.01
    row = [date, sv1, az1, sp1, pf1, mod1, sv2, az2, sp2, pf2, mod2, sv3, az3, sp3, pf3, mod3]
    cols = [f"Vac Given {m} {d} Cum" for d in [1, 2, 3] for m in ["Sinovac", "AstraZeneca", "Sinopharm", "Pfizer", "Moderna"]]
    row = pd.DataFrame([row], columns=['Date'] + cols)
    logger.info("{} Vac slides {} {}", date.date(), file, row.to_string(header=False, index=False))
    return df.combine_first(row.set_index("Date"))


def vac_slides_groups(df, page, file, page_num):
    if "กลุ่มเปา้หมาย" not in page:
        return
    # does fairly good job
    table = camelot_cache(file, page_num, process_background=False)
    table = table[2:]
    for i in range(1, 7):
        table[i] = pd.to_numeric(table[i].str.replace(",", "").replace("-", "0"))
    table.columns = ["group", "1 Cum", "1", "2 Cum", "2", "3 Cum", "3"]
    table.loc[:, "group"] = [
        "Vac Group Medical Staff",
        "Vac Group Health Volunteer",
        "Vac Group Other Frontline Staff",
        "Vac Group Over 60",
        "Vac Group Risk: Disease",
        "Vac Group Risk: Pregnant",
        "Vac Group Risk: Location",
        "Vac Group Student"
        "Total"
    ]
    table.pivot(columns="group", values=["1 Cum", "2 Cum", "3 Cum"])

    # medical, rest = get_next_numbers(page, "บคุลากรทางการแพ", until="\n")
    # village, rest = get_next_numbers(rest, "เจา้หน้าทีด่", until="\n")
    # disease, rest = get_next_numbers(rest, "ผู้มีโรคเรือ้รัง 7", until="\n")
    # public, rest = get_next_numbers(rest, "ประชาชนทัว่ไป", until="\n")
    # over60, rest = get_next_numbers(rest, "ผู้มีอาย ุ60", until="\n")
    # pregnant, rest = get_next_numbers(rest, "หญิงตัง้ครรภ์", until="\n")
    # total, rest = get_next_numbers(rest, "รวม", until="\n")

# จ านวนการได้รับวัคซีนโควิด 19 ของประเทศไทย แยกตามกลุ่มเป้าหมาย
# สะสมตั้งแต่วันที่ 28 กุมภาพันธ์ – 9 สิงหาคม 2564


# ที่มา : ฐานข้อมูลกระทรวงสาธารณสุข (MOPH  Immunization Center) ข้อมูล ณ วันที่ 9 สิงหาคม 2564 เวลา 18.00 น.


# เขม็ที่ 1 (คน)  เพ่ิมขึน้ (คน) เขม็ที่ 2 (คน)  เพ่ิมขึน้ (คน) เขม็ที่ 3 (คน)  เพ่ิมขึน้ (คน)


# บคุลากรทางการแพทยแ์ละสาธารณสุข 832,908           5,042              718,384           4,308              268,022           46,457
# เจา้หน้าทีด่า่นหน้า 945,171           8,475              560,922           4,676              -                  -
# อาสาสมัครสาธารณสุขประจ าหมูบ่า้น 530,994           7,943              234,800           3,197              -                  -
# ผู้มีโรคเรือ้รัง 7 กลุ่มโรค 1,795,485        44,910            306,421           8,472              -                  -
# ประชาชนทัว่ไป 8,811,064        204,868           2,514,032        72,179            -                  -
# ผู้มีอาย ุ60 ปขีึน้ไป 3,414,683        78,160            231,332           11,514            -                  -
# หญิงตัง้ครรภ์ 6,438              991                 454                 138                 -                  -


# รวม 16,336,743      350,389           4,566,345        104,484           268,022           46,457


# กลุ่มเปา้หมาย
# จ านวนผู้ที่ไดร้ับวคัซีน

def vac_slides_files(check=True):
    folders = [f"https://ddc.moph.go.th/vaccine-covid19/diaryPresentMonth/{m}/10/2021" for m in range(1, 12)]
    links = sorted((link for f in folders for link in web_links(f, ext=".pdf", check=check)), reverse=True)
    count = 0
    for link in links:
        if USE_CACHE_DATA and count > MAX_DAYS:
            break
        count += 1

        def dl_file(link=link):
            file, _, _ = next(iter(web_files(link, dir="inputs/vaccinations")))
            return file

        yield link, None, dl_file


def vac_slides():
    df = pd.DataFrame(columns=['Date']).set_index("Date")
    for link, _, get_file in vac_slides_files():
        file = get_file()
        for i, page in enumerate(parse_file(file), 1):
            # pass
            df = vac_manuf_given(df, page, file, i, link)
            #df = vac_slides_groups(df, page, file, i)
    return df
