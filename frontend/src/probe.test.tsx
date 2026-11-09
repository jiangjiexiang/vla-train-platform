import { render } from '@testing-library/react';
import { Form, Input, InputNumber } from 'antd';
import { expect, it } from 'vitest';

it('probe', async () => {
  const { container } = render(
    <Form initialValues={{ params: { a: 5, b: 'x' } }}>
      <Form.Item name={['params', 'a']} label="A"><InputNumber /></Form.Item>
      <Form.Item name={['params', 'b']} label="B"><Input /></Form.Item>
    </Form>,
  );
  await new Promise((r) => setTimeout(r, 100));
  console.log('PROBE number=', (container.querySelector('.ant-input-number input') as HTMLInputElement)?.value,
    'text=', (container.querySelector('.ant-input input') as HTMLInputElement)?.value);
  expect(true).toBe(true);
});
