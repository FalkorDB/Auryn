<?php

namespace App\Web;

trait IdentifiableTrait
{
    protected string $id = '';

    public function id(): string
    {
        return $this->id;
    }
}
