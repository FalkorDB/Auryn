<?php

namespace App\Web;

use App\Models\Status;
use App\Models\Store;

class Widget extends Component implements Renderable
{
    use IdentifiableTrait;

    public const MAX = 10;

    private Status $status;

    public function __construct(private Store $store, private string $label)
    {
        $this->status = Status::Active;
    }

    public function save(): void
    {
        $this->store->put($this->render());
    }

    public function render(): string
    {
        return $this->label;
    }
}
