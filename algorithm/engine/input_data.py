import pandas as pd
import numpy as np
import os
import re

ddl = 62


# === [修复 1] 补上缺失的 safe_float 函数 ===
def safe_float(val):
    """
    安全地将提取的值转换为浮点数。
    如果遇到 '——', '-', 'nan' 或空值，自动返回 0.0
    """
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


# ==========================================

def get_pieces_per_box(spec):
    """
    通过正则表达式解析药品规格，计算出一盒/一瓶有多少片或粒。
    例如："0.5mg×10片×2板×400盒" -> 10片 * 2板 = 20片/盒
    """
    spec = str(spec).replace(' ', '')

    # 提取单板/单瓶的片数或粒数
    piece_match = re.search(r'(\d+)(片|粒)', spec)
    pieces = int(piece_match.group(1)) if piece_match else 1

    # 提取每盒的板数
    board_match = re.search(r'(\d+)板', spec)
    boards = int(board_match.group(1)) if board_match else 1

    # 总片数 = 单板片数 * 板数
    return pieces * boards


def split_drug_item(item):
    parts = str(item).strip().split(' ', 1)
    if len(parts) == 1:
        return parts[0], ''
    return parts[0], parts[1]


def extract_dose_feature(spec):
    spec = str(spec).strip()

    bracket_match = re.match(r'^(（[^）]*?(?:mg|g|μg|ug)[^）]*?）)', spec, flags=re.IGNORECASE)
    if bracket_match:
        return bracket_match.group(1)

    dose_match = re.match(
        r'^([0-9]+(?:\.[0-9]+)?\s*(?:mg|g|μg|ug)'
        r'(?:\s*[:：/]\s*[0-9]+(?:\.[0-9]+)?\s*(?:mg|g|μg|ug))?)',
        spec,
        flags=re.IGNORECASE
    )
    if dose_match:
        return dose_match.group(1).replace(' ', '')

    return ''


def extract_piece_feature(spec):
    spec = str(spec).strip()
    piece_match = re.search(r'(\d+)\s*(片|粒)', spec)
    if piece_match:
        return f"{piece_match.group(1)}{piece_match.group(2)}"
    return ''


def get_mix_spec(item):
    name, spec = split_drug_item(item)
    dose = extract_dose_feature(spec)
    return f"{name} {dose}" if dose else name


def get_pack_spec(item):
    name, spec = split_drug_item(item)
    piece = extract_piece_feature(spec)
    return f"{name} {piece}" if piece else name


REMOVED_TABLET_MACHINES = {"菲特P2020", "菲特P3030"}
NEW_TABLET_MACHINES = ["压片一", "压片二", "压片三", "压片四", "压片五", "压片六"]
SPLIT_MIX_LINES = {
    "2-1、2-2线": ["2-1线", "2-2线"],
}

NEW_TABLET_MACHINE_BY_DRUG = {
    "缬沙坦氢氯噻嗪片": ["压片一", "压片三"],
    "阿司匹林肠溶片": ["压片一", "压片二", "压片四", "压片五"],
    "盐酸二甲双胍缓释片": ["压片一", "压片三", "压片四"],
    "左氧氟沙星片": ["压片三", "压片六"],
    "瑞巴派特片": ["压片三"],
    "阿德福韦酯片": ["压片一", "压片三"],
    "甲钴胺片": ["压片一", "压片三", "压片五"],
    "缬沙坦氨氯地平片": ["压片三", "压片五"],
    "伏格列波糖片": ["压片三"],
    "盐酸伊托必利片": ["压片四"],
    "非诺地平缓释片": ["压片六"],
    "西格列汀二甲双胍片": ["压片三"],
    "维生素B1片": ["压片五"],
    "雷贝拉唑钠肠溶片": ["压片一"],
    "盐酸鲁拉西酮片": ["压片三"],
    "美沙拉嗪肠溶片": ["压片三"],
    "氯吡格雷阿司匹林片": ["压片二"],
    "富马酸丙酚替诺福韦片": ["压片三"],
    "苯立氟胶片": ["压片三"],
    "帕利哌酮缓释片": ["压片六"],
    "达格列净二甲双胍缓释片": ["压片四"],
}


def get_new_tablet_machines(drug_name):
    drug_name = str(drug_name).strip()
    for name, machines in NEW_TABLET_MACHINE_BY_DRUG.items():
        if drug_name == name or drug_name.startswith(name) or name in drug_name:
            return machines
    return []


def expand_mix_lines(line_name):
    line_name = str(line_name).strip()
    return SPLIT_MIX_LINES.get(line_name, [line_name])


def _build_aps_columns(header_l1, header_l2):
    columns_l1 = header_l1.ffill().tolist()
    columns_l2 = header_l2.tolist()
    cols = []
    for i, (c1, c2) in enumerate(zip(columns_l1, columns_l2)):
        c1_str = str(c1).strip() if pd.notna(c1) else ""
        c2_str = str(c2).strip() if pd.notna(c2) else ""
        if c1_str and c2_str:
            cols.append(f"{c1_str}_{c2_str}")
        elif c1_str:
            cols.append(c1_str)
        elif c2_str:
            cols.append(c2_str)
        else:
            cols.append(f"Unnamed_{i}")
    return cols


def read_aps_table(aps_file, sheet_name='APS生产信息统计'):
    raw = pd.read_excel(aps_file, sheet_name=sheet_name, header=None)

    header_row = None
    for idx in range(len(raw) - 1):
        row_values = {str(value).strip() for value in raw.iloc[idx].dropna().tolist()}
        if {'品种', '包装规格'}.issubset(row_values):
            header_row = idx
            break

    if header_row is None:
        raise ValueError(f"APS表未找到包含'品种'和'包装规格'的表头行: {aps_file}")

    cols = _build_aps_columns(raw.iloc[header_row], raw.iloc[header_row + 1])
    df = raw.iloc[header_row + 2:].copy()
    df.columns = cols
    df = df.dropna(how='all')

    required_cols = {'品种', '包装规格'}
    missing_cols = required_cols - set(df.columns)
    if missing_cols:
        raise ValueError(f"APS表缺少必要列: {', '.join(sorted(missing_cols))}")

    df['品种'] = df['品种'].ffill().astype(str).str.strip()
    df['包装规格'] = df['包装规格'].astype(str).str.strip()

    fill_cols = [
        '配料_配料线体', '配料_批量（万片/粒）', '配料_班产量（万片）',
        '压片_压片机', '压片_班产量', '包衣_包衣机', '包衣_班产量',
        '分装/铝塑_操作间及设备', '分装/铝塑_班产量（万片）',
        '生产周期/天', '是否集采品种', '年销量/万'
    ]
    for col in fill_cols:
        if col in df.columns:
            df[col] = df[col].replace(r'^\s*$', np.nan, regex=True)
            df[col] = df.groupby('品种')[col].ffill()

    return df


def build_schedule_inputs(
    demo_file,
    aps_file,
    stage_staff_limits_override=None,
    max_continuous_run_override=None,
    major_cleaning_time_override=None,
    minor_cleaning_time_override=None,
    department="210车间",
):
    """
    根据 demo 订单表和 APS 产能表，自动提取并构建排产算法所需的输入参数。
    目前已实现: I, J1, J2, J3, J4, B, p1, p2
    """
    # ==========================================
    # 0. 定义主数据映射字典 (ERP/Demo -> APS车间)
    # ==========================================
    name_mapping = {
        "阿司匹林肠溶片（过评）": "阿司匹林肠溶片100mg",
        "左氧氟沙星片(片剂)": "左氧氟沙星片",
        "琥珀酸美托洛尔缓释胶囊(硬胶囊)": "琥珀酸美托洛尔胶囊",
        "缬沙坦氨氯地平片(片剂)": "缬沙坦氨氯地平片",
        "盐酸伊托必利片(片剂)": "盐酸伊托必利片",
        "缬沙坦氢氯噻嗪片（过评）": "缬沙坦氢氯噻嗪片",
        "瑞巴派特片(片剂)": "瑞巴派特片",
        "艾司奥美拉唑镁肠溶胶囊(硬胶囊)": "艾美",
        "盐酸二甲双胍缓释片（过评）": "盐酸二甲双胍缓释片",
        "非洛地平缓释片": "非诺地平缓释片",
    }

    spec_mapping = {
        "47.5mg×14粒×2板×400盒": "47.5mg×14片×2板×400盒",
        "10mg×100片×600瓶/10瓶*60包": "100片/瓶*600瓶/箱",
        "80mg×7片×4板×400盒": "7片×4板×400盒",
        "50mg×10片×2板×400盒": "10片×2板×400盒",
        "80mg×14片×2板×400盒": "（80mg:12.5mg）14片×2板×400盒",
        "0.5gx30片x400瓶": "30片/瓶×400盒/箱",
        "40mg*7粒/板*4板/盒*200盒": "40mg×7粒×4板×200盒",
        "20mg*7粒/板*4板/盒*200盒": "20mg×7粒×4板×200盒",
        "0.2g×10片×1板×400盒": "10粒×1板×400盒",
        "0.2g×10片×2板×400盒": "10粒×2板×400盒",
        "0.5mg×12粒×2板×400盒": "0.5mg×12片×2板×400盒",
        "25mg（按C21H29N6O5P计）×15片×4板×400盒": "15片×4板×400盒",
        "23.75mg×14粒×2板×400盒": "23.75mg×14片×2板×400盒",
        "5mg*10片/板*4板*400盒": "10片×4板×400盒",
    }

    # ==========================================
    # 1. 构造集合 I (提取指定车间排产需求)
    # ==========================================
    if not os.path.exists(demo_file):
        raise FileNotFoundError(f"找不到文件：{demo_file}")
    df_demo = pd.read_excel(demo_file)

    df_210 = df_demo[df_demo['部门'] == department].copy()
    df_210 = df_210.dropna(subset=['存货名称', '规格'])
    df_210['存货名称'] = df_210['存货名称'].astype(str).str.strip()
    df_210['规格'] = df_210['规格'].astype(str).str.strip()

    df_210['APS_名称'] = df_210['存货名称'].replace(name_mapping)
    df_210['APS_规格'] = df_210['规格'].replace(spec_mapping)

    unique_items = df_210[['APS_名称', 'APS_规格']].drop_duplicates()

    # ==========================================
    # 🌟 新增：前置数据筛选层（在源头直接剔除本月没有生产计划的规格）
    # ==========================================
    valid_rows = []
    for _, row in unique_items.iterrows():
        # 安全聚合该规格在订单表中的总计划产量（使用精准的一个下划线列名）
        amt_series = df_210[(df_210['APS_名称'] == row['APS_名称']) & (df_210['APS_规格'] == row['APS_规格'])][
            '月份生产计划']
        total_amt = pd.to_numeric(amt_series, errors='coerce').sum()

        # 只有当产量不为空且大于 0 时，才保留该药品规格
        if pd.notna(total_amt) and total_amt > 0:
            valid_rows.append(row)

    # 重新构建过滤后的唯一规格清单
    if valid_rows:
        unique_items = pd.DataFrame(valid_rows)
    else:
        unique_items = pd.DataFrame(columns=['APS_名称', 'APS_规格'])

    I = []
    for _, row in unique_items.iterrows():
        I.append(f"{row['APS_名称']} {row['APS_规格']}")

    # ==========================================
    # 2. 读取 APS 表格及合并单元格清洗
    # ==========================================
    if not os.path.exists(aps_file):
        raise FileNotFoundError(f"找不到文件：{aps_file}")
    df_aps = read_aps_table(aps_file)

    # 填充所有关键列，防止合并单元格导致 nan
    fill_cols = [
        '配料_配料线体', '配料_批量（万片/粒）', '配料_班产量（万片）',
        '压片_压片机', '压片_班产量', '包衣_包衣机', '包衣_班产量',
        '分装/铝塑_操作间及设备', '分装/铝塑_班产量（万片）',
        '生产周期/天', '是否集采品种', '年销量/万'
    ]
    for col in fill_cols:
        if col in df_aps.columns:
            df_aps[col] = df_aps[col].replace(r'^\s*$', np.nan, regex=True)
            df_aps[col] = df_aps.groupby('品种')[col].ffill()

    # 提取产线集合
    J1 = []
    for x in df_aps['配料_配料线体'].dropna().unique():
        line = str(x).strip()
        if line in ['nan', '-', '']:
            continue
        for expanded_line in expand_mix_lines(line):
            if expanded_line not in J1:
                J1.append(expanded_line)
    J2 = [
        str(x).strip()
        for x in df_aps['压片_压片机'].dropna().unique()
        if str(x).strip() not in ['nan', '-', ''] and str(x).strip() not in REMOVED_TABLET_MACHINES
    ]
    for machine in NEW_TABLET_MACHINES:
        if machine not in J2:
            J2.append(machine)
    J3 = [str(x).strip() for x in df_aps['包衣_包衣机'].dropna().unique() if str(x).strip() not in ['nan', '-', '', '无']]
    J4 = [str(x).strip() for x in df_aps['分装/铝塑_操作间及设备'].dropna().unique() if
          str(x).strip() not in ['nan', '-', '']]

    # ==========================================
    # 3. 构造 B, p1, p2 逻辑
    # ==========================================
    I = []
    B = {}
    p1 = {}
    p2 = {}
    p3 = {}
    p4 = {}
    p5 = {}
    d = {}
    T = {}
    w = {}
    sort_data = []

    for _, row in unique_items.iterrows():
        aps_name = row['APS_名称']
        aps_spec = row['APS_规格']
        key = f"{aps_name} {aps_spec}"

        match_aps = df_aps[(df_aps['品种'] == aps_name) & (df_aps['包装规格'] == aps_spec)]

        # 如果 APS 表里没有这个药，就直接跳过，既不进 I 也不进 B
        if match_aps.empty:
            print(f"⚠️ 警告：药品 [{key}] 在 APS 产能表中未找到匹配，已跳过。")
            continue

        # 匹配成功，将该药品加入排产集合 I
        I.append(key)  # <--- 修改点 2：匹配成功才加入 I

    for _, row in unique_items.iterrows():
        aps_name = row['APS_名称']
        aps_spec = row['APS_规格']
        key = f"{aps_name} {aps_spec}"

        match_aps = df_aps[(df_aps['品种'] == aps_name) & (df_aps['包装规格'] == aps_spec)]
        if match_aps.empty:
            continue

        # 数值b: 批量
        batch_size_10k = float(match_aps['配料_批量（万片/粒）'].iloc[0]) if pd.notna(
            match_aps['配料_批量（万片/粒）'].iloc[0]) else 0
        b = batch_size_10k * 10000

        # 计算集合 B
        target_series = df_210[(df_210['APS_名称'] == aps_name) & (df_210['APS_规格'] == aps_spec)]['月份生产计划']
        target_boxes = pd.to_numeric(target_series, errors='coerce').sum()
        pieces_per_box = get_pieces_per_box(aps_spec)
        total_pieces = target_boxes * pieces_per_box

        batches = 1
        if b > 0:
            batches = max(1, int((total_pieces / b) + 0.5))
        B[key] = list(range(1, batches + 1))

        # --- p1 逻辑 (配料) ---
        mix_shift_output = float(match_aps['配料_班产量（万片）'].iloc[0]) if pd.notna(
            match_aps['配料_班产量（万片）'].iloc[0]) else 0
        a_mix = mix_shift_output * 10000
        c_mix = b / a_mix if a_mix > 0 else 0
        mix_line = str(match_aps['配料_配料线体'].iloc[0]).strip()
        if mix_line and mix_line != 'nan':
            for expanded_line in expand_mix_lines(mix_line):
                p1[(key, expanded_line)] = c_mix

        # --- p2 逻辑 (压片) ---
        tablet_shift_output = float(match_aps['压片_班产量'].iloc[0]) if pd.notna(
            match_aps['压片_班产量'].iloc[0]) else 0
        a_tablet = tablet_shift_output * 10000  # 数a
        c_tablet = b / a_tablet if a_tablet > 0 else 0  # 数c
        tablet_machine = str(match_aps['压片_压片机'].iloc[0]).strip()
        if tablet_machine and tablet_machine != 'nan' and tablet_machine not in REMOVED_TABLET_MACHINES:
            p2[(key, tablet_machine)] = c_tablet
        for tablet_machine in get_new_tablet_machines(aps_name):
            p2[(key, tablet_machine)] = c_tablet

        # --- p3 逻辑 (包衣) ---
        raw_coat_output = match_aps['包衣_班产量'].iloc[0]

        # 针对 '——' 或空值进行安全判断，不直接强转 float
        if pd.notna(raw_coat_output) and str(raw_coat_output).strip() not in ['-', '——', 'nan', '']:
            a_coat = float(raw_coat_output) * 10000
        else:
            a_coat = 0

        # 计算数 c
        c_coat = b / a_coat if a_coat > 0 else 0

        coat_machine = str(match_aps['包衣_包衣机'].iloc[0]).strip()
        if c_coat > 0 and coat_machine and coat_machine not in ['nan', '-', '——', '', '无']:
            p3[(key, coat_machine)] = c_coat

        # --- p4 逻辑 (分装/铝塑) ---
        raw_pack_output = match_aps['分装/铝塑_班产量（万片）'].iloc[0]

        if pd.notna(raw_pack_output) and str(raw_pack_output).strip() not in ['-', '——', 'nan', '']:
            a_pack = float(raw_pack_output) * 10000
        else:
            a_pack = 0

        c_pack = b / a_pack if a_pack > 0 else 0

        pack_machine = str(match_aps['分装/铝塑_操作间及设备'].iloc[0]).strip()
        if pack_machine and pack_machine not in ['nan', '-', '——', '']:
            p4[(key, pack_machine)] = c_pack

        # --- p5 逻辑 ---
        p5[key] = len(B[key]) * c_pack

        d[key] = ddl

        # --- T 逻辑 (生产周期) ---
        raw_cycle = match_aps['生产周期/天'].iloc[0]
        if pd.notna(raw_cycle) and str(raw_cycle).strip() not in ['-', '——', 'nan', '']:
            T[key] = float(raw_cycle) * 2
        else:
            T[key] = 0

        # --- 收集 w 排序所需的数据 ---
        if 'U8现存量' in df_210.columns:
            u8_stock = pd.to_numeric(
                df_210[(df_210['APS_名称'] == aps_name) & (df_210['APS_规格'] == aps_spec)]['U8现存量'],
                errors='coerce').sum()
        else:
            u8_stock = 1.0

        is_jicai = str(match_aps['是否集采品种'].iloc[0]).strip()
        annual_sales = safe_float(match_aps['年销量/万'].iloc[0])

        sort_data.append({
            'key': key,
            'u8_stock': u8_stock,
            'is_jicai': is_jicai,
            'annual_sales': annual_sales
        })
        # ====================== for 循环到此结束 ======================

    # # 特殊处理-琥珀酸
    # B['琥珀酸美托洛尔胶囊 47.5mg×14片×2板×400盒'] = [1,2,3,4,5,6,7,8]

    # === [修复 2] 将排序逻辑移出 for 循环 ===
    # ==========================================
    # 4. 执行 w 的多级排序与倒序赋权逻辑
    # ==========================================
    def get_sort_tuple(item):
        c1 = (item['u8_stock'] != 0)
        c2 = (item['is_jicai'] != '是')
        c3 = -item['annual_sales']
        return (c1, c2, c3)

    # 执行排序
    sort_data.sort(key=get_sort_tuple)

    # 获取一共有多少个不同的排名层级
    unique_ranks = set(get_sort_tuple(item) for item in sort_data)
    max_weight = len(unique_ranks)

    current_rank_tuple = None
    for item in sort_data:
        rank_tuple = get_sort_tuple(item)
        if rank_tuple != current_rank_tuple:
            current_rank_tuple = rank_tuple
            current_weight = max_weight
            max_weight -= 1
        w[item['key']] = current_weight * current_weight

    # ==========================================
    # 5. 新需求参数：人员、清场矩阵、释放时间、产线可用时间、连续开机上限
    # ==========================================
    stage_staff_limits = {
        1: 4,  # 配料
        2: 3,  # 压片
        3: 3,  # 包衣
        4: 4,  # 包装/铝塑
    }
    if stage_staff_limits_override is not None:
        stage_staff_limits.update({int(k): int(v) for k, v in stage_staff_limits_override.items()})

    mix_specs = {item: get_mix_spec(item) for item in I}
    pack_specs = {item: get_pack_spec(item) for item in I}

    def build_clear_matrix(feature_map, clear_time):
        return {
            (from_item, to_item): (0 if feature_map[from_item] == feature_map[to_item] else clear_time)
            for from_item in I
            for to_item in I
        }

    major_cleaning_time = 1.0 if major_cleaning_time_override is None else float(major_cleaning_time_override)
    minor_cleaning_time = 0.5 if minor_cleaning_time_override is None else float(minor_cleaning_time_override)
    clear_time_matrices = {
        1: build_clear_matrix(mix_specs, major_cleaning_time),
        2: build_clear_matrix(mix_specs, major_cleaning_time),
        3: build_clear_matrix(mix_specs, major_cleaning_time),
        4: build_clear_matrix(pack_specs, minor_cleaning_time),
    }

    machine_available_time = {
        j: 0
        for j in (J1 + J2 + J3 + J4)
    }

    release_time = {
        (i, b): 0
        for i in I
        for b in B[i]
    }

    max_continuous_run = 11 if max_continuous_run_override is None else float(max_continuous_run_override)

    return (
        I, J1, J2, J3, J4, B, p1, p2, p3, p4, p5, d, T, w,
        stage_staff_limits, clear_time_matrices, machine_available_time,
        release_time, max_continuous_run
    )


# ================== 测试代码 ==================
if __name__ == "__main__":
    DEMO_FILE = "./输入/2026年07月车间分解编排计划--20260712.xlsx"
    APS_FILE = "./输入/副本APS排产信息-4.30.xlsx"

    # 测试执行
    (
        I, J1, J2, J3, J4, B, p1, p2, p3, p4, p5, d, T, w,
        stage_staff_limits, clear_time_matrices, machine_available_time,
        release_time, max_continuous_run
    ) = build_schedule_inputs(DEMO_FILE, APS_FILE)

    print(I)

    print("\n--- 提取到的产线集合 J1 (配料线) ---")
    print(J1)

    print("\n--- 提取到的产线集合 J2 (压片线) ---")
    print(J2)

    print("\n--- 提取到的产线集合 J3 (包衣线) ---")
    print(J3)

    print("\n--- 提取到的产线集合 J4 (包装/铝塑线) ---")
    print(J4)

    print("\n--- 提取到的批次集合 B ---")

    for k, v in B.items():
        print(f"'{k}': {v}")

    print("\n--- 提取到的p1班时 ---")

    for k, v in p1.items():
        print(f"'{k}': {v}")

    print("\n--- 提取到的p2班时 ---")

    for k, v in p2.items():
        print(f"'{k}': {v}")

    print("\n--- 提取到的p3班时 ---")

    for k, v in p3.items():
        print(f"'{k}': {v}")

    print("\n--- 提取到的p4班时 ---")

    for k, v in p4.items():
        print(f"'{k}': {v}")

    print("\n--- 提取到的p5班时 ---")

    for k, v in p5.items():
        print(f"'{k}': {v}")

    print("\n--- 提取到的d ---")

    for k, v in d.items():
        print(f"'{k}': {v}")

    print("\n--- 提取到的T ---")

    for k, v in T.items():
        print(f"'{k}': {v}")

    print("\n--- 提取到的W ---")

    for k, v in w.items():
        print(f"'{k}': {v}")

    print("\n--- 新需求参数 ---")
    print("人员上限:", stage_staff_limits)
    print("连续开机上限:", max_continuous_run)
    print("清场矩阵规模:", {k: len(v) for k, v in clear_time_matrices.items()})
