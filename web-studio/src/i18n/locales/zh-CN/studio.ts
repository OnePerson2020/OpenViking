const studio = {
  selectSessionRecord: '从左侧选择一个会话，查看其当前上下文。',
  noSessionRecords: '当前身份没有可查看的会话记录。',
  sessionRecords: '会话记录',
  processingTasks: '处理任务',
  hostedSessionHistory:
    '仅展示当前上下文消息，历史归档未加载。提交过的会话可能没有当前消息。',
  backToFiles: '返回文件列表',
  showOverview: '展开统计与队列',
  hideOverview: '收起统计与队列',
  connectionSummary: {
    opensource:
      '当前工作区按开源服务展示；服务扩展和管理功能需对应配置与权限。',
    volcengine:
      '当前工作区按火山托管服务展示；仅显示检测可用的扩展功能，用户 API Key 不提供管理授权。',
    unknown: '连接验证成功后会展示对应功能。自定义地址请指定服务类型。',
  },

  watchesReadDescription: '查看已配置的定时同步任务及运行状态。',
  watchesReadEmpty: '当前没有可访问的定时同步任务。',
  statistics: '统计分析',
  listingLimit: '当前视图最多展示 500 项。',
  directory: '我的目录',
  memories: '记忆',
  domainsLabel: 'Studio 功能域',
  domains: { workspace: '工作区', extensions: '高级功能', management: '管理' },
  providers: {
    opensource: '开源服务',
    volcengine: '火山托管',
    unknown: '服务类型待确认',
    checking: '正在检测连接…',
  },
  serviceType: '服务类型',
  auto: '自动检测',
  serviceHint:
    '验证 API Key 后，自动识别火山官方地址和本地服务。自定义域名或代理地址请指定服务类型；版本号本身不能确定服务类型。',
  cloudManagement:
    '火山管理功能需要连接独立管理服务。数据 API Key 不会自动开放管理权限。',
  connectionSettings: '连接设置',
  retry: '重试',
  loading: '正在加载…',
  empty: '当前目录为空。',
  selectFile: '选择文件以预览内容。',
  spaces: '目录范围',
  personal: '我的空间',
  shared: '共享资源',
  back: '返回',
  readOnly: '当前工作区为只读。共享资源与个人空间分开展示。',
  directoryDescription: '浏览自己的上下文，以及获得授权的共享资源。',
  directoryError: '无法打开目录：目录可能不存在，或当前身份没有访问权限。',
  states: {
    checking: {
      title: '正在验证可用性',
      description: '正在验证当前连接及读取能力。',
    },
    connectionFailed: {
      title: '连接尚未验证成功',
      description: '请检查服务地址和 API Key，再到连接设置中重试。',
    },
    managementRequired: {
      title: '需要独立管理授权',
      description:
        '开源管理功能需要管理连接与对应权限。火山管理功能需要独立管理后端。',
    },
    unknownProvider: {
      title: '请确认服务类型',
      description:
        '该自定义地址没有明确的服务标识，请在连接设置中指定服务类型后继续使用。',
    },
    unavailable: {
      title: '当前服务未开放此功能',
      description: '你仍可浏览自己的目录并使用其他可用功能。',
    },
    forbidden: {
      title: '当前身份没有访问权限',
      description: '当前连接没有授予此功能需要的读取权限。',
    },
    unknown: {
      title: '此功能的可用性尚未确认',
      description: '当前连接的扩展接口契约尚未验证，不影响公共工作区。',
    },
  },
} as const
export default studio
