<?php

namespace App\Models;

abstract class AbstractStore
{
    abstract public function size(): int;

    public function isEmpty(): bool
    {
        return $this->size() === 0;
    }
}
