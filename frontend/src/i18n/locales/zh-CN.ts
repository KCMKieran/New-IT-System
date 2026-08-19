// Chinese (Simplified) translations
export const zhCN = {
  // Common
  common: {
    search: "搜索",
    settings: "设置",
    help: "帮助",
    loading: "加载中...",
    error: "错误",
    success: "成功",
    cancel: "取消",
    confirm: "确认",
    save: "保存",
    delete: "删除",
    edit: "编辑",
    add: "添加",
    close: "关闭",
    comma: "，",
  },

  // Navigation
  nav: {
    customerPnLMonitorV2: "交易账户 - 盈亏监控",
    clientPnLMonitor: "客户 - 盈亏监控",
    // ibReport: "IB 业绩报表", // [REMOVED]
    // equityMonitor: "Equity - Monitor", // [REMOVED]
    clientTrading: "客户交易分析",
    swapFreeControl: "Swap Free Control",
    basisAnalysis: "基差分析",
    // downloads: "数据下载", // [REMOVED]
    warehouse: "报仓数据",
    warehouseProducts: "产品交易汇总",
    ibData: "IB / 地区出入金",
    position: "实时持仓",
    warehouseOthers: "其他",
    loginIPs: "MT LoginIP 监测",
    profitAnalysis: "利润分析",
    template: "模板",
    agentGlobal: "代理统计Global",
    customerPnLMonitor: "客户盈亏监控",
    others: "其他",
    getHelp: "获取帮助",
    csDepartment: "CS Department",
    // [REMOVED] globalCsDepartment - merged into csDepartment
    riskControlDepartment: "Risk Control",
    otherSection: "Other",
    dataQuery: "Data Query",
    holdBucketReport: "持仓时间分析",
    ibidLots: "IB及旗下客户交易查询",
    clientReturnRate: "客户收益率",
    ibFinancialMonitor: "IB 资金监控",
    fundFlowMonitor: "频繁出入金监控",
    ibTreeQuery: "IB Tree查询",
    riskAlertMail: "告警邮件中心",
    // These three risk pages keep English labels in both locales — the Chinese
    // names were too similar to tell apart at a glance (user decision 2026-08-05).
    riskMonitor: "Risk Rule Alerts",
    riskWatchlist: "Client Activity Monitor",
    windowScan: "Entry Window Scan",
    home: "首页",
    // Auth P4b: tooltip on the greyed-out /docs/ entry. The entry stays visible
    // for everyone — a vanished link reads as "the docs were deleted".
    docsManagerOnly: "文档站仅管理员可访问，请联系管理员开通",
  },

  // Auth P4b — module names, as shown on the 403 page. Must match the labels
  // managers tick in /cfg/managers, or the person asking for access and the
  // person granting it are talking about different things.
  modules: {
    dashboard: "Dashboard（首页）",
    cs: "CS Department（客服部）",
    data: "Data Query（数据查询）",
    risk: "Risk Control（风险控制）",
    other: "Other（其他）",
  },

  // Auth P4b — the 403 page. Written for someone whose next action is to ask
  // for access, so it names what is missing and who grants it.
  forbidden: {
    title: "无权访问此页面",
    moduleBody: "这个页面属于「{module}」模块，你的账号还没有获得该模块的权限。",
    managerBody: "这个页面仅管理员（manager）可见。",
    genericBody: "你的账号没有访问这个页面的权限。",
    askManager: "请联系管理员在「Managers」页面为你勾选相应权限；页面权限由管理员逐人分配。",
    backHome: "返回首页",
  },

  // Page titles
  pages: {
    home: "首页",
    homeWelcome: "欢迎使用 KCM Analytics System，请从左侧导航选择功能模块。",
    template: "模板",
    // equityMonitor: "Equity - Monitor", // [REMOVED]
    goldQuote: "黄金报价",
    basisAnalysis: "基差分析",
    // downloads: "数据下载", // [REMOVED]
    warehouseAgentGlobal: "代理统计Global",
    warehouse: "报仓数据",
    warehouseProducts: "产品交易汇总",
    ibData: "IB / 地区出入金",
    warehouseOthers: "其他报仓",
    position: "实时持仓",
    loginIPs: "Login IP监测",
    profitAnalysis: "利润分析",
    clientTrading: "客户交易分析",
    // ibReport: "IB 业绩报表", // [REMOVED]
    riskMonitor: "Risk Rule Alerts", // English in both locales — see nav block
    swapFreeControl: "Swap Free Control",
    customerPnLMonitor: "客户盈亏监控",
    customerPnLMonitorV2: "客户盈亏监控（V2）",
    settings: "设置",
    search: "搜索",
    configuration: "配置",
    viewProfiles: "View Profiles",
    ibidLots: "IBID 手数查询",
    clientReturnRate: "客户收益率",
    holdBucketReport: "持仓时间分析",
    ibFinancialMonitor: "IB 资金监控",
    ibTreeQuery: "IB Tree查询",
    riskAlertMail: "告警邮件中心",
    riskWatchlist: "Client Activity Monitor", // English in both locales — see nav block
    windowScan: "Entry Window Scan", // English in both locales — see nav block
  },

  ibidLotsPage: {
    description:
      "按 ibid / 用户 id / 交易账户统计一段区间内的成交手数，并拆出「持仓 ≥10s / <10s」。",
  },

  // Configuration
  config: {
    docs: "Documents",
    // Auth P4a: stays English in the zh-CN locale too (Kieran, 2026-08-14), so
    // the sidebar entry matches the "Managers" heading on the page it opens.
    managers: "Managers",
    // English in zh-CN too, matching the page heading (same rule as `managers`).
    viewProfiles: "View Profiles",
    customGroups: "自定义组别",
    reports: "Reports",
    financial: "Financial",
    clients: "Clients",
    tasks: "Tasks",
    marketing: "Marketing",
  },

  // Site header
  header: {
    title: "KCM Analytics System",
  },

  // Customer P&L Monitor V2 page
  pnlMonitor: {
    title: "客户盈亏监控 - 筛选",
    server: "服务器",
    selectServer: "选择服务器",
    group: "组别",
    selectGroup: "选择组别...",
    loadingGroups: "加载组别中...",
    searchGroup: "搜索组别...",
    search: "搜索",
    searchPlaceholder: "账户ID，姓名，ClientID",
    refresh: "刷新",
    refreshing: "刷新中...",
    serverNotSupported: "仅支持 MT5/MT4Live2 服务器刷新",
    serverNotConnected: "该服务器暂未接入",
    loadFailed: "加载失败",
    refreshFailed: "刷新失败",
    totalRecords: "共 {count} 条记录",
    currentPage: "当前页 {current}/{total}",
    sortBy: "排序: {sort}",
    dataUpdateTime: "数据更新时间（UTC+8）：{time}",
    totalRecordsDisplay: "显示 {start} 到 {end} 条，共 {total} 条记录",
    perPage: "每页显示",
    records: "条",
    firstPage: "首页",
    prevPage: "上一页",
    nextPage: "下一页",
    lastPage: "末页",
    pageInfo: "第 {current} 页，共 {total} 页",
    filter: "筛选",
    columnToggle: "列显示切换",
    showColumns: "显示列",
    clearAll: "清空所有",
    filterConditions: "筛选条件 ({join}):",
    timeRange: "账户活跃时间",
    timeRangeAll: "全部时间",
    timeRange1w: "过去 1 周",
    timeRange2w: "过去 2 周",
    timeRange1m: "过去 1 个月",
    timeRange3m: "过去 3 个月",
    // Column headers
    columns: {
      login: "账户ID",
      userName: "客户名称",
      userGroup: "Group",
      country: "国家/地区",
      zipcode: "ZipCode",
      userId: "ClientID",
      symbol: "Symbol",
      balance: "balance",
      floatingPnL: "持仓浮动盈亏",
      equity: "equity",
      closedSellVolume: "closed_sell_volume_lots",
      closedSellCount: "closed_sell_count",
      closedSellProfit: "closed_sell_profit",
      closedSellSwap: "closed_sell_swap",
      closedSellOvernightCount: "closed_sell_overnight_count",
      closedSellOvernightVolume: "closed_sell_overnight_volume_lots",
      closedBuyVolume: "closed_buy_volume_lots",
      closedBuyCount: "closed_buy_count",
      closedBuyProfit: "closed_buy_profit",
      closedBuySwap: "closed_buy_swap",
      closedBuyOvernightCount: "closed_buy_overnight_count",
      closedBuyOvernightVolume: "closed_buy_overnight_volume_lots",
      totalCommission: "total_commission",
      depositCount: "入金笔数",
      depositAmount: "入金金额",
      withdrawalCount: "出金笔数",
      withdrawalAmount: "出金金额",
      netDeposit: "net_deposit",
      closedTotalProfit: "平仓总盈亏（包含swap）",
      overnightVolumeRatio: "过夜成交占比",
      overnightVolumeAll: "过夜订单手数",
      totalVolumeAll: "总订单手数",
      overnightOrderAll: "过夜订单数",
      totalOrderAll: "总订单数",
      lastUpdated: "更新时间",
      overnight: "过夜",
      overnightVolumeRatioHeader: "过夜成交量占比",
      volume: "手数",
      orders: "订单",
    },
    // Refresh messages
    refreshMessages: {
      processedTrades: "处理{count}新交易",
      updatedFloating: "更新{count}条浮动盈亏",
      duration: "耗时 {seconds} 秒",
      completed: "刷新完成",
    },
  },

  // Authentication (auth design P3 — Entra ID single sign-on)
  auth: {
    title: "登录",
    subtitle: "使用公司 Microsoft 账户登录",
    signInWithMicrosoft: "使用 Microsoft 账户登录",
    accessNote: "仅限已获授权的 KCM 员工账户。如无法登录，请联系 IT。",
    signOut: "退出登录",
    account: "账户",
    errors: {
      notAuthorized: "该账户无权访问本系统。如需开通，请联系 IT。",
      idpRefused: "Microsoft 拒绝了本次登录。通常是该账户尚未被指派到本应用，请联系 IT。",
      noEmailClaim: "无法从 Microsoft 获取邮箱地址，这是目录配置问题，请联系 IT。",
      expired: "登录已超时，请重新登录。",
      providerDisabled: "登录服务未配置，请联系 IT。",
      generic: "登录失败，请重试。若持续失败，请联系 IT。",
    },
  },
} as const;
