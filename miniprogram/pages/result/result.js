// pages/result/result.js
const app = getApp()

Page({
  data: {
    description: '',
    loading: true,
    recommendations: [],
    disclaimer: '本结果仅供参考，实际归类以海关核定为准'
  },

  onLoad(options) {
    const description = decodeURIComponent(options.description || '')
    this.setData({ description })
    this.classify(description)
  },

  classify(description) {
    const apiBase = app.globalData.apiBase
    wx.request({
      url: `${apiBase}/api/classify`,
      method: 'POST',
      data: { description },
      header: { 'content-type': 'application/json' },
      timeout: 30000,
      success: (res) => {
        if (res.statusCode === 200 && res.data) {
          const recs = res.data.recommendations || []
          this.setData({
            recommendations: recs,
            disclaimer: res.data.disclaimer || this.data.disclaimer
          })
          // 保存历史记录
          if (recs.length > 0) {
            app.addHistory({
              description,
              topResult: recs[0]
            })
          }
        } else {
          this.setData({ recommendations: [] })
        }
      },
      fail: () => {
        wx.showToast({ title: '网络错误，请重试', icon: 'none' })
        this.setData({ recommendations: [] })
      },
      complete: () => {
        this.setData({ loading: false })
      }
    })
  },

  goBack() {
    wx.navigateBack()
  },

  copyCode(e) {
    const code = e.currentTarget.dataset.code
    wx.setClipboardData({
      data: code,
      success: () => {
        wx.showToast({ title: '已复制编码', icon: 'success' })
      }
    })
  }
})
