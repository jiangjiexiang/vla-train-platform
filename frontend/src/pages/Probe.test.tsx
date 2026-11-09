import { fireEvent, render } from '@testing-library/react';
import { App as AntdApp, Form, Input, InputNumber } from 'antd';
import { expect, it } from 'vitest';

function Comp() {
  const [form] = Form.useForm();
  return (
    <AntdApp>
      <Form form={form} initialValues={{ name: '', params: { steps: 5 } }}>
        <Form.Item name="name" label="任务名" rules={[{ pattern: /^[a-z0-9-]+$/, message: 'bad' }]}>
          <Input />
        </Form.Item>
        <Form.Item name={['params', 'steps']} label="steps" rules={[{ required: false }]}>
          <InputNumber />
        </Form.Item>
        <Form.Item name={['params', 'steps2']} label="steps2">
          <InputNumber />
        </Form.Item>
      </Form>
    </AntdApp>
  );
}

it('ids', async () => {
  const { container } = render(<Comp />);
  await new Promise((r) => setTimeout(r, 50));
  const labels = [...container.querySelectorAll('label')].map((l) => `${l.textContent}->${l.getAttribute('for')}`);
  const inputs = [...container.querySelectorAll('input')].map((i) => JSON.stringify({ id: i.id, v: i.value }));
  console.log('IDS', labels.join(' | '), '\nINPUTS', inputs.join(' '));
  expect(true).toBe(true);
});
