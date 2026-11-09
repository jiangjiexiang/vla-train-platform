import { render } from '@testing-library/react';
import { App, Form, InputNumber, Input } from 'antd';
import { expect, it } from 'vitest';

function Trial({ variant }: { variant: number }) {
  const [form] = Form.useForm();
  return (
    <App>
      <Form form={form} initialValues={variant === 3 ? undefined : { params: { a: 5, s: 'x' } }}>
        <Form.Item name={['params', 'a']} label="A"><InputNumber /></Form.Item>
        <Form.Item name={['params', 's']} label="S"><Input /></Form.Item>
      </Form>
    </App>
  );
}
it('trials', async () => {
  for (const v of [1, 2, 3]) {
    const { container, unmount } = render(<Trial variant={v} />);
    await new Promise((r) => setTimeout(r, 50));
    const num = container.querySelector('.ant-input-number input') as HTMLInputElement;
    const text = container.querySelectorAll('input')[1] as HTMLInputElement;
    console.log('TRIAL', v, 'num=', JSON.stringify(num?.value), 'text=', JSON.stringify(text?.value));
    unmount();
  }
  expect(true).toBe(true);
});
