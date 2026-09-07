// pages/result/result.js
const app = getApp()

Page({
  data: {
    description: '',
    loading: true,
    recommendations: [],
    disclaimer: '本结果仅供参考，实际归类以海关核定为准',
    serviceUnavailable: false,
    feedbackEmail: ''
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
          const isUnavailable = res.data.status === 'service_unavailable'
          this.setData({
            recommendations: recs,
            disclaimer: res.data.disclaimer || this.data.disclaimer,
            serviceUnavailable: isUnavailable,
            feedbackEmail: (res.data.feedback && res.data.feedback.email) || ''
          })
          if (recs.length > 0) {
            app.addHistory({
              description,
              topResult: recs[0]
            })
          }
        } else {
          this.setData({ recommendations: [], serviceUnavailable: true })
        }
      },
      fail: () => {
        this.setData({
          recommendations: [],
          serviceUnavailable: true,
          disclaimer: '网络连接失败，请检查网络后重试。如持续出现此问题，请通过下方方式反馈。',
          feedbackEmail: 'hq15012670635@163.com'
        })
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
  },

  copyFeedbackEmail() {
    if (!this.data.feedbackEmail) return
    wx.setClipboardData({
      data: this.data.feedbackEmail,
      success: () => {
        wx.showToast({ title: '邮箱已复制', icon: 'success' })
      }
    })
  },

  retry() {
    this.setData({ loading: true, recommendations: [], serviceUnavailable: false })
    this.classify(this.data.description)
  }
})
