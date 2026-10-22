import { describe, expect, it } from 'vitest';
import { ApiError } from '../api/client';
import { describeError, isVersionConflict, resolvePagingAfterConflict } from './conflict';

const indexChanged = () => new ApiError(409, 'SEARCH_INDEX_CHANGED', '资料已更新，请用相同条件重新查询第一页。');
const versionConflict = () => new ApiError(409, 'UNIT_VERSION_CONFLICT', '这一页已被其他改动更新，请重新核对。');

describe('409 的两种解释', () => {
  it('SEARCH_INDEX_CHANGED 归类为索引代次变化，保留条件回到第一页', () => {
    const info = describeError(indexChanged());
    expect(info.kind).toBe('index-changed');
    expect(info.keepDraft).toBe(true);

    const resolution = resolvePagingAfterConflict(indexChanged());
    expect(resolution).not.toBeNull();
    expect(resolution?.next).toEqual({ page: 1, expectedIndexRevision: null });
    expect(resolution?.notice).toContain('索引代次');
  });

  it('其它 409 属于版本冲突，必须保留草稿', () => {
    const info = describeError(versionConflict());
    expect(info.kind).toBe('version-conflict');
    expect(info.keepDraft).toBe(true);
    expect(isVersionConflict(versionConflict())).toBe(true);
    // 版本冲突不应触发检索翻页重置
    expect(resolvePagingAfterConflict(versionConflict())).toBeNull();
  });
});

describe('其它错误', () => {
  it('断线归类为 offline 并保留草稿', () => {
    const info = describeError(new ApiError(0, 'NETWORK', '无法连接本地文献服务'));
    expect(info.kind).toBe('offline');
    expect(info.keepDraft).toBe(true);
  });

  it('普通 HTTP 错误带上状态与消息', () => {
    const info = describeError(new ApiError(400, 'VALIDATION', '印刷页码不能为空'));
    expect(info.kind).toBe('http');
    expect(info.status).toBe(400);
    expect(info.message).toBe('印刷页码不能为空');
  });

  it('取消请求被识别为 aborted，不弹错误', () => {
    const info = describeError(new DOMException('aborted', 'AbortError'));
    expect(info.kind).toBe('aborted');
  });

  it('未知异常也保留草稿', () => {
    const info = describeError(new Error('boom'));
    expect(info.kind).toBe('unknown');
    expect(info.keepDraft).toBe(true);
  });
});